#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "board_audio.h"
#include "driver/gpio.h"
#include "esp_heap_caps.h"
#include "esp_check.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_sntp.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mbedtls/md.h"
#include "nvs_flash.h"
#include "esp_afe_config.h"
#include "esp_afe_sr_iface.h"
#include "esp_vad.h"
#include "esp_wn_iface.h"
#include "model_path.h"

#include "sdkconfig.h"

#define WIFI_CONNECTED_BIT BIT0
#define MAX_CAPTURE_SECONDS 28
#define MAX_CAPTURE_BYTES (SAGE_AUDIO_RATE * 2 * MAX_CAPTURE_SECONDS)
#define MIC_LEVEL_CHANNEL 0
#define MIC_NO_SPEECH_TIMEOUT_MS 7000
#define END_OF_SPEECH_MS 1100
#define FOLLOWUP_LISTEN_MS 6000
#define BUTTON_GPIO GPIO_NUM_0

static const char *TAG = "sage-speaker";
static EventGroupHandle_t wifi_events;
static SemaphoreHandle_t audio_stream_mutex;
static volatile bool speaker_active;
static volatile int64_t followup_until_ms;
static volatile int64_t suppress_wake_until_ms;
static int16_t *capture_pcm;
static int16_t playback_samples[1024];
static uint8_t pcm_carry;
static bool has_pcm_carry;
static const esp_afe_sr_iface_t *afe;
static esp_afe_sr_data_t *afe_data;

typedef struct {
    bool locked;
    bool play_response;
    bool *listen;
} http_audio_context_t;

static void check(esp_err_t err, const char *message)
{
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "%s: %s", message, esp_err_to_name(err));
        abort();
    }
}

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        esp_wifi_connect();
        xEventGroupClearBits(wifi_events, WIFI_CONNECTED_BIT);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(wifi_events, WIFI_CONNECTED_BIT);
    }
}

static void wifi_start(void)
{
    wifi_events = xEventGroupCreate();
    check(esp_netif_init(), "network init");
    check(esp_event_loop_create_default(), "event loop");
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t wifi_config = WIFI_INIT_CONFIG_DEFAULT();
    check(esp_wifi_init(&wifi_config), "Wi-Fi init");
    check(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL),
          "Wi-Fi event handler");
    check(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL),
          "IP event handler");

    wifi_config_t station = {0};
    strlcpy((char *)station.sta.ssid, CONFIG_SAGE_WIFI_SSID, sizeof(station.sta.ssid));
    strlcpy((char *)station.sta.password, CONFIG_SAGE_WIFI_PASSWORD, sizeof(station.sta.password));
    station.sta.threshold.authmode = WIFI_AUTH_OPEN;
    check(esp_wifi_set_mode(WIFI_MODE_STA), "Wi-Fi mode");
    check(esp_wifi_set_config(WIFI_IF_STA, &station), "Wi-Fi config");
    check(esp_wifi_start(), "Wi-Fi start");
    check(esp_wifi_connect(), "Wi-Fi connect");
    xEventGroupWaitBits(wifi_events, WIFI_CONNECTED_BIT, pdFALSE, pdTRUE, portMAX_DELAY);
}

static void clock_start(void)
{
    esp_sntp_setoperatingmode(SNTP_OPMODE_POLL);
    esp_sntp_setservername(0, "pool.ntp.org");
    esp_sntp_init();
    for (int attempt = 0; attempt < 30 && time(NULL) < 1700000000; ++attempt) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    if (time(NULL) < 1700000000) {
        ESP_LOGE(TAG, "NTP time sync failed; retrying until signed requests can be authenticated");
        while (time(NULL) < 1700000000) {
            vTaskDelay(pdMS_TO_TICKS(1000));
        }
    }
}

static void bytes_to_hex(const uint8_t *input, size_t length, char *output)
{
    static const char digits[] = "0123456789abcdef";
    for (size_t i = 0; i < length; ++i) {
        output[i * 2] = digits[input[i] >> 4];
        output[i * 2 + 1] = digits[input[i] & 0x0f];
    }
    output[length * 2] = '\0';
}

static esp_err_t sign_request(const char *timestamp, const char *method, const char *path,
                              const uint8_t *body, size_t body_size, char signature[65])
{
    uint8_t body_hash[32];
    uint8_t mac[32];
    char body_hash_hex[65];
    char message[512];
    const mbedtls_md_info_t *sha256 = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    if (!sha256 || mbedtls_md(sha256, body, body_size, body_hash) != 0) {
        return ESP_FAIL;
    }
    bytes_to_hex(body_hash, sizeof(body_hash), body_hash_hex);
    int written = snprintf(message, sizeof(message), "%s.%s.%s.%s", timestamp, method, path,
                           body_hash_hex);
    if (written < 0 || (size_t)written >= sizeof(message)) {
        return ESP_FAIL;
    }
    const char *secret = CONFIG_SAGE_VOICE_SECRET;
    if (!secret[0] || mbedtls_md_hmac(sha256, (const unsigned char *)secret, strlen(secret),
                                      (const unsigned char *)message, (size_t)written, mac) != 0) {
        return ESP_FAIL;
    }
    bytes_to_hex(mac, sizeof(mac), signature);
    return ESP_OK;
}

static esp_err_t add_auth_headers(esp_http_client_handle_t client, const char *method,
                                  const char *path, const uint8_t *body, size_t body_size)
{
    char timestamp[20];
    char signature[65];
    snprintf(timestamp, sizeof(timestamp), "%lld", (long long)time(NULL));
    ESP_RETURN_ON_ERROR(sign_request(timestamp, method, path, body, body_size, signature),
                        TAG, "request signature");
    ESP_RETURN_ON_ERROR(esp_http_client_set_header(client, "X-Sage-Time", timestamp),
                        TAG, "timestamp header");
    return esp_http_client_set_header(client, "X-Sage-Sig", signature);
}

static void play_pcm_bytes(const uint8_t *data, size_t length)
{
    if (length == 0) {
        return;
    }
    size_t input_offset = 0;
    if (has_pcm_carry && length) {
        uint8_t pair[2] = {pcm_carry, data[0]};
        memcpy(playback_samples, pair, sizeof(pair));
        input_offset = 1;
        has_pcm_carry = false;
        board_audio_write(playback_samples, 1);
    }
    size_t remaining = length - input_offset;
    size_t even_bytes = remaining & ~(size_t)1;
    size_t offset = input_offset;
    while (even_bytes) {
        size_t block = even_bytes > sizeof(playback_samples) ? sizeof(playback_samples) : even_bytes;
        memcpy(playback_samples, data + offset, block);
        if (board_audio_write(playback_samples, block / 2) != ESP_OK) {
            ESP_LOGW(TAG, "Speaker write failed");
            break;
        }
        offset += block;
        even_bytes -= block;
    }
    if (offset < length) {
        pcm_carry = data[offset];
        has_pcm_carry = true;
    }
}

static esp_err_t http_event(esp_http_client_event_t *event)
{
    http_audio_context_t *audio = event->user_data;
    if (!audio) {
        return ESP_OK;
    }
    if (event->event_id == HTTP_EVENT_ON_HEADER && audio->listen &&
        event->header_key && event->header_value &&
        strcmp(event->header_key, "X-Sage-Listen") == 0) {
        *audio->listen = strcmp(event->header_value, "1") == 0;
    } else if (event->event_id == HTTP_EVENT_ON_DATA && audio->play_response &&
               event->data && event->data_len > 0 &&
               esp_http_client_get_status_code(event->client) == 200) {
        if (!audio->locked) {
            xSemaphoreTake(audio_stream_mutex, portMAX_DELAY);
            audio->locked = true;
            speaker_active = true;
            pcm_carry = 0;
            has_pcm_carry = false;
        }
        play_pcm_bytes((const uint8_t *)event->data, (size_t)event->data_len);
    }
    return ESP_OK;
}

static esp_http_client_handle_t new_client(const char *path, const char *method,
                                           const uint8_t *body, size_t body_size,
                                           bool post_audio, http_audio_context_t *audio)
{
    char url[192];
    snprintf(url, sizeof(url), "http://%s:%d%s", CONFIG_SAGE_VOICE_HOST,
             CONFIG_SAGE_VOICE_PORT, path);
    const esp_http_client_config_t config = {
        .url = url,
        .event_handler = http_event,
        .user_data = audio,
        .timeout_ms = 45000,
        .buffer_size = 4096,
        .buffer_size_tx = 4096,
        .keep_alive_enable = false,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) {
        return NULL;
    }
    esp_http_client_set_method(client, strcmp(method, "POST") == 0 ? HTTP_METHOD_POST : HTTP_METHOD_GET);
    if (body_size) {
        if (esp_http_client_set_post_field(client, (const char *)body, (int)body_size) != ESP_OK ||
            esp_http_client_set_header(client, "Content-Type",
                                       post_audio ? "application/octet-stream" : "application/json") != ESP_OK) {
            esp_http_client_cleanup(client);
            return NULL;
        }
    }
    if (add_auth_headers(client, method, path, body, body_size) != ESP_OK) {
        esp_http_client_cleanup(client);
        return NULL;
    }
    return client;
}

static int perform_request(const char *path, const char *method, const uint8_t *body,
                           size_t body_size, bool post_audio, bool play_response, bool *listen)
{
    if (listen) {
        *listen = false;
    }
    http_audio_context_t audio = {
        .play_response = play_response,
        .listen = listen,
    };
    esp_http_client_handle_t client = new_client(
        path, method, body, body_size, post_audio,
        (play_response || listen) ? &audio : NULL);
    if (!client) {
        ESP_LOGE(TAG, "Could not create or sign request for %s", path);
        return -1;
    }
    esp_err_t err = esp_http_client_perform(client);
    int status = err == ESP_OK ? esp_http_client_get_status_code(client) : -1;
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "%s failed: %s", path, esp_err_to_name(err));
    } else if (status >= 400) {
        ESP_LOGE(TAG, "%s returned HTTP %d", path, status);
    }
    esp_http_client_cleanup(client);
    if (audio.locked) {
        speaker_active = false;
        suppress_wake_until_ms = esp_timer_get_time() / 1000 + 800;
        has_pcm_carry = false;
        xSemaphoreGive(audio_stream_mutex);
    }
    return status;
}

static void report_ring(const char *state)
{
    char body[32];
    int size = snprintf(body, sizeof(body), "{\"state\":\"%s\"}", state);
    perform_request("/api/voice/ring", "POST", (const uint8_t *)body, (size_t)size, false, false,
                    NULL);
}

static void send_utterance(size_t byte_count)
{
    if (byte_count < SAGE_AUDIO_RATE * 2 / 3) {
        report_ring("idle");
        return;
    }
    ESP_LOGI(TAG, "Sending %.1f seconds of audio", (float)byte_count / (SAGE_AUDIO_RATE * 2));
    bool listen = false;
    int status = perform_request("/api/voice/ask", "POST", (uint8_t *)capture_pcm,
                                byte_count, true, true, &listen);
    report_ring("idle");
    if (status == 200 && listen) {
        ESP_LOGI(TAG, "Follow-up listening enabled for %d seconds", FOLLOWUP_LISTEN_MS / 1000);
        followup_until_ms = esp_timer_get_time() / 1000 + FOLLOWUP_LISTEN_MS;
    }
}

static void feed_frame(int16_t *raw, int16_t *mono, int chunk_samples)
{
    if (board_audio_read(raw, (size_t)chunk_samples * SAGE_MIC_TDM_CHANNELS) != ESP_OK) {
        ESP_LOGE(TAG, "Microphone read failed");
        vTaskDelay(pdMS_TO_TICKS(10));
        return;
    }
    for (int i = 0; i < chunk_samples; ++i) {
        mono[i] = raw[i * SAGE_MIC_TDM_CHANNELS + MIC_LEVEL_CHANNEL];
    }
    afe->feed(afe_data, mono);
}

static size_t capture_after_wake(int16_t *raw, int16_t *mono, int chunk_samples)
{
    size_t bytes = 0;
    int64_t first_speech_at = 0;
    int64_t last_speech_at = 0;
    int64_t capture_started_at = esp_timer_get_time() / 1000;
    bool speech_seen = false;
    bool cache_added = false;
    ESP_LOGI(TAG, "Wake word detected; listening");

    while (bytes + SAGE_AUDIO_RATE / 2 < MAX_CAPTURE_BYTES) {
        feed_frame(raw, mono, chunk_samples);
        afe_fetch_result_t *result = afe->fetch_with_delay(afe_data, pdMS_TO_TICKS(100));
        if (!result || result->ret_value == ESP_FAIL || speaker_active ||
            !result->data || result->data_size <= 0) {
            continue;
        }
        int64_t now = esp_timer_get_time() / 1000;
        if (result->vad_state == VAD_SPEECH) {
            if (!speech_seen) {
                speech_seen = true;
                first_speech_at = now;
            }
            last_speech_at = now;
        } else if (!speech_seen && now - capture_started_at >= MIC_NO_SPEECH_TIMEOUT_MS) {
            break;
        }

        if (speech_seen && !cache_added && result->vad_cache && result->vad_cache_size > 0) {
            size_t cache_size = (size_t)result->vad_cache_size;
            if (cache_size <= MAX_CAPTURE_BYTES) {
                memcpy((uint8_t *)capture_pcm + bytes, result->vad_cache, cache_size);
                bytes += cache_size;
            }
            cache_added = true;
        }
        if (speech_seen && bytes + (size_t)result->data_size <= MAX_CAPTURE_BYTES) {
            memcpy((uint8_t *)capture_pcm + bytes, result->data, (size_t)result->data_size);
            bytes += (size_t)result->data_size;
        }
        if (speech_seen && now - last_speech_at >= END_OF_SPEECH_MS &&
            now - first_speech_at >= 350) {
            break;
        }
        if (speech_seen && bytes >= MAX_CAPTURE_BYTES - (size_t)result->data_size) {
            break;
        }
    }
    return bytes;
}

static void audio_task(void *arg)
{
    int chunk_samples = afe->get_feed_chunksize(afe_data);
    int channels = afe->get_feed_channel_num(afe_data);
    int16_t *raw = heap_caps_malloc((size_t)chunk_samples * SAGE_MIC_TDM_CHANNELS *
                                        sizeof(int16_t),
                                    MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    int16_t *mono = heap_caps_malloc((size_t)chunk_samples * channels * sizeof(int16_t),
                                     MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!raw || !mono) {
        ESP_LOGE(TAG, "Not enough memory for microphone buffers");
        vTaskDelete(NULL);
        return;
    }
    capture_pcm = heap_caps_malloc(MAX_CAPTURE_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!capture_pcm) {
        ESP_LOGE(TAG, "Could not allocate capture buffer");
        vTaskDelete(NULL);
        return;
    }

    for (;;) {
        feed_frame(raw, mono, chunk_samples);
        afe_fetch_result_t *result = afe->fetch_with_delay(afe_data, pdMS_TO_TICKS(100));
        if (!result || result->ret_value == ESP_FAIL || speaker_active) {
            continue;
        }
        int64_t now = esp_timer_get_time() / 1000;
        bool followup = now < followup_until_ms;
        if (now >= suppress_wake_until_ms &&
            (result->wakeup_state == WAKENET_DETECTED ||
             (followup && result->vad_state == VAD_SPEECH))) {
            followup_until_ms = 0;
            size_t captured = capture_after_wake(raw, mono, chunk_samples);
            send_utterance(captured);
        }
    }
}

static void poll_task(void *arg)
{
    for (;;) {
        int status = perform_request("/api/voice/poll?wait=25", "GET", NULL, 0, false, true, NULL);
        if (status < 0) {
            vTaskDelay(pdMS_TO_TICKS(3000));
        } else if (status >= 400) {
            ESP_LOGE(TAG, "Pausing voice polling after HTTP %d to avoid repeated failed requests", status);
            vTaskDelay(pdMS_TO_TICKS(600000));
        }
    }
}

static void button_task(void *arg)
{
    gpio_config_t config = {
        .pin_bit_mask = 1ULL << BUTTON_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    check(gpio_config(&config), "BOOT button");
    bool pressed = false;
    int64_t pressed_at = 0;
    for (;;) {
        bool down = gpio_get_level(BUTTON_GPIO) == 0;
        int64_t now = esp_timer_get_time() / 1000;
        if (down && !pressed) {
            pressed = true;
            pressed_at = now;
        } else if (!down && pressed) {
            pressed = false;
            const char *press = now - pressed_at >= 800 ? "long" : "short";
            char body[24];
            int size = snprintf(body, sizeof(body), "{\"press\":\"%s\"}", press);
            perform_request("/api/voice/button", "POST", (const uint8_t *)body,
                            (size_t)size, false, true, NULL);
        }
        vTaskDelay(pdMS_TO_TICKS(40));
    }
}

static void wake_engine_start(void)
{
    srmodel_list_t *models = esp_srmodel_init("model");
    if (!models) {
        ESP_LOGE(TAG, "No ESP-SR model partition. Select Hi,ESP and build the model image.");
        abort();
    }
    bool has_wake_word = false;
    for (int i = 0; i < models->num; ++i) {
        if (strcmp(models->model_name[i], "wn9_hiesp") == 0) {
            has_wake_word = true;
            break;
        }
    }
    if (!has_wake_word) {
        ESP_LOGE(TAG, "WakeNet model wn9_hiesp is missing; select Hi,ESP in menuconfig.");
        abort();
    }

    afe_config_t *config = afe_config_init("M", models, AFE_TYPE_SR, AFE_MODE_HIGH_PERF);
    if (!config) {
        ESP_LOGE(TAG, "AFE configuration failed");
        abort();
    }
    config->wakenet_init = true;
    config->wakenet_model_name = "wn9_hiesp";
    config->vad_init = true;
    config->vad_min_speech_ms = 128;
    config->vad_min_noise_ms = 700;
    afe = esp_afe_handle_from_config(config);
    afe_data = afe ? afe->create_from_config(config) : NULL;
    afe_config_free(config);
    if (!afe || !afe_data) {
        ESP_LOGE(TAG, "ESP-SR audio front end initialization failed");
        abort();
    }
    ESP_LOGI(TAG, "WakeNet Hi,ESP is ready");
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        check(nvs_flash_erase(), "erase NVS");
        err = nvs_flash_init();
    }
    check(err, "NVS init");
    if (!CONFIG_SAGE_WIFI_SSID[0] || !CONFIG_SAGE_VOICE_SECRET[0]) {
        ESP_LOGE(TAG, "Set Wi-Fi credentials and the Sage HMAC secret in menuconfig");
        abort();
    }

    audio_stream_mutex = xSemaphoreCreateMutex();
    if (!audio_stream_mutex) {
        ESP_LOGE(TAG, "Could not create audio stream mutex");
        abort();
    }
    check(board_audio_init(), "Audio board init");
    wifi_start();
    clock_start();
    wake_engine_start();
    xTaskCreatePinnedToCore(audio_task, "sage-audio", 8192, NULL, 8, NULL, 1);
    xTaskCreatePinnedToCore(poll_task, "sage-poll", 6144, NULL, 5, NULL, 0);
    xTaskCreatePinnedToCore(button_task, "sage-button", 3072, NULL, 4, NULL, 0);
    ESP_LOGI(TAG, "Sage speaker ready; say Hi ESP to speak");
}

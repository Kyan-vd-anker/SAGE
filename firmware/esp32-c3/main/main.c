#include <stdio.h>
#include <stdlib.h>
#include <stdbool.h>
#include <string.h>
#include <time.h>

#include "esp_check.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_sntp.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "mbedtls/md.h"
#include "nvs_flash.h"
#include "sage_ble.h"
#include "sdkconfig.h"

#define WIFI_CONNECTED_BIT BIT0
#define PRESENCE_PATH "/api/voice/presence"
#define REPORT_INTERVAL_MS 20000
#define SIGHTING_MAX_AGE_MS 15000

static const char *TAG = "sage-presence";

static void check(esp_err_t err, const char *message)
{
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "%s: %s", message, esp_err_to_name(err));
        abort();
    }
}

#if CONFIG_SAGE_BLE_RECEIVER
static EventGroupHandle_t wifi_events;

static bool valid_node_id(const char *node)
{
    if (!node || !node[0] || strlen(node) > 40) {
        return false;
    }
    for (const char *character = node; *character; ++character) {
        if (!((*character >= 'a' && *character <= 'z') ||
              (*character >= 'A' && *character <= 'Z') ||
              (*character >= '0' && *character <= '9') ||
              *character == '-' || *character == '_')) {
            return false;
        }
    }
    return true;
}

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(wifi_events, WIFI_CONNECTED_BIT);
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(wifi_events, WIFI_CONNECTED_BIT);
    }
}

static void wifi_start(void)
{
    wifi_events = xEventGroupCreate();
    if (!wifi_events) {
        ESP_LOGE(TAG, "Could not create Wi-Fi event group");
        abort();
    }
    check(esp_netif_init(), "network init");
    check(esp_event_loop_create_default(), "event loop");
    if (!esp_netif_create_default_wifi_sta()) {
        ESP_LOGE(TAG, "Could not create Wi-Fi station interface");
        abort();
    }
    wifi_init_config_t init_config = WIFI_INIT_CONFIG_DEFAULT();
    check(esp_wifi_init(&init_config), "Wi-Fi init");
    check(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL),
          "Wi-Fi event handler");
    check(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL),
          "IP event handler");

    wifi_config_t station = {0};
    strlcpy((char *)station.sta.ssid, CONFIG_SAGE_PRESENCE_WIFI_SSID,
            sizeof(station.sta.ssid));
    strlcpy((char *)station.sta.password, CONFIG_SAGE_PRESENCE_WIFI_PASSWORD,
            sizeof(station.sta.password));
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
    while (time(NULL) < 1700000000) {
        vTaskDelay(pdMS_TO_TICKS(1000));
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

static esp_err_t sign_request(const char *timestamp, const char *body, char signature[65])
{
    uint8_t body_hash[32];
    uint8_t mac[32];
    char body_hash_hex[65];
    char message[192];
    const mbedtls_md_info_t *sha256 = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    if (!sha256 || mbedtls_md(sha256, (const uint8_t *)body, strlen(body), body_hash) != 0) {
        return ESP_FAIL;
    }
    bytes_to_hex(body_hash, sizeof(body_hash), body_hash_hex);
    int written = snprintf(message, sizeof(message), "%s.POST.%s.%s", timestamp,
                           PRESENCE_PATH, body_hash_hex);
    if (written < 0 || (size_t)written >= sizeof(message) ||
        mbedtls_md_hmac(sha256, (const uint8_t *)CONFIG_SAGE_PRESENCE_SECRET,
                        strlen(CONFIG_SAGE_PRESENCE_SECRET), (const uint8_t *)message,
                        (size_t)written, mac) != 0) {
        return ESP_FAIL;
    }
    bytes_to_hex(mac, sizeof(mac), signature);
    return ESP_OK;
}

static esp_err_t report_presence(void)
{
    int rssi = sage_ble_last_rssi(SIGHTING_MAX_AGE_MS);
    char body[112];
    int body_size = rssi == -128
                        ? snprintf(body, sizeof(body), "{\"node\":\"%s\",\"rssi\":null}",
                                   CONFIG_SAGE_BLE_NODE_ID)
                        : snprintf(body, sizeof(body), "{\"node\":\"%s\",\"rssi\":%d}",
                                   CONFIG_SAGE_BLE_NODE_ID, rssi);
    if (body_size < 0 || (size_t)body_size >= sizeof(body)) {
        return ESP_ERR_INVALID_SIZE;
    }
    char timestamp[20];
    char signature[65];
    snprintf(timestamp, sizeof(timestamp), "%lld", (long long)time(NULL));
    ESP_RETURN_ON_ERROR(sign_request(timestamp, body, signature), TAG, "request signature");

    char url[128];
    int url_size = snprintf(url, sizeof(url), "http://%s:%d%s",
                            CONFIG_SAGE_PRESENCE_SERVER_IP,
                            CONFIG_SAGE_PRESENCE_SERVER_PORT, PRESENCE_PATH);
    if (url_size < 0 || (size_t)url_size >= sizeof(url)) {
        return ESP_ERR_INVALID_SIZE;
    }
    esp_http_client_config_t client_config = {
        .url = url,
        .timeout_ms = 5000,
        .keep_alive_enable = false,
    };
    esp_http_client_handle_t client = esp_http_client_init(&client_config);
    if (!client) {
        return ESP_ERR_NO_MEM;
    }
    esp_err_t err = esp_http_client_set_method(client, HTTP_METHOD_POST);
    if (err == ESP_OK) {
        err = esp_http_client_set_header(client, "Content-Type", "application/json");
    }
    if (err == ESP_OK) {
        err = esp_http_client_set_header(client, "X-Sage-Time", timestamp);
    }
    if (err == ESP_OK) {
        err = esp_http_client_set_header(client, "X-Sage-Sig", signature);
    }
    if (err == ESP_OK) {
        err = esp_http_client_set_post_field(client, body, body_size);
    }
    if (err == ESP_OK) {
        err = esp_http_client_perform(client);
    }
    if (err == ESP_OK) {
        int status = esp_http_client_get_status_code(client);
        if (status < 200 || status >= 300) {
            ESP_LOGE(TAG, "Presence request returned HTTP %d", status);
            err = ESP_FAIL;
        }
    } else {
        ESP_LOGE(TAG, "Presence request failed: %s", esp_err_to_name(err));
    }
    esp_http_client_cleanup(client);
    return err;
}

static void report_task(void *arg)
{
    for (;;) {
        if (xEventGroupGetBits(wifi_events) & WIFI_CONNECTED_BIT) {
            esp_err_t err = report_presence();
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "Could not report BLE presence: %s", esp_err_to_name(err));
            }
        }
        vTaskDelay(pdMS_TO_TICKS(REPORT_INTERVAL_MS));
    }
}
#endif

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        check(nvs_flash_erase(), "erase NVS");
        err = nvs_flash_init();
    }
    check(err, "NVS init");

#if CONFIG_SAGE_BLE_BEACON
    check(sage_ble_start_beacon(), "BLE beacon");
    ESP_LOGI(TAG, "Sage presence beacon is advertising");
#else
    if (!CONFIG_SAGE_PRESENCE_WIFI_SSID[0] || !CONFIG_SAGE_PRESENCE_SECRET[0] ||
        !valid_node_id(CONFIG_SAGE_BLE_NODE_ID)) {
        ESP_LOGE(TAG, "Set Wi-Fi credentials, HMAC secret, and a valid node ID in menuconfig");
        abort();
    }
    wifi_start();
    clock_start();
    check(sage_ble_start_scanner(), "BLE scanner");
    if (xTaskCreate(report_task, "sage-presence-report", 4096, NULL, 3, NULL) != pdPASS) {
        ESP_LOGE(TAG, "Could not create presence reporter task");
        abort();
    }
    ESP_LOGI(TAG, "Sage BLE room receiver is ready");
#endif
}

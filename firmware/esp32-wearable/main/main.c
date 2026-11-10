#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/i2c.h"
#include "esp_event.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

#define WIFI_CONNECTED BIT0
#define I2C_PORT I2C_NUM_0
#define OLED_WIDTH 128
#define OLED_HEIGHT 64
#define OLED_PAGE_COUNT 8

static const char *TAG = "sage-wearable";
static EventGroupHandle_t wifi_events;
static httpd_handle_t setup_server;
static bool setup_mode;
static bool listening;
static char wifi_ssid[33];
static char wifi_password[65];
static char sage_host[64];
static char sage_secret[129];

static esp_err_t nvs_read_string(nvs_handle_t handle, const char *key, char *out, size_t size)
{
    size_t length = size;
    esp_err_t err = nvs_get_str(handle, key, out, &length);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        out[0] = '\0';
        return ESP_OK;
    }
    return err;
}

static esp_err_t load_settings(void)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open("wearable", NVS_READONLY, &handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        wifi_ssid[0] = '\0';
        wifi_password[0] = '\0';
        sage_host[0] = '\0';
        sage_secret[0] = '\0';
        return ESP_OK;
    }
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_read_string(handle, "wifi_ssid", wifi_ssid, sizeof(wifi_ssid));
    if (err == ESP_OK) err = nvs_read_string(handle, "wifi_password", wifi_password, sizeof(wifi_password));
    if (err == ESP_OK) err = nvs_read_string(handle, "sage_host", sage_host, sizeof(sage_host));
    if (err == ESP_OK) err = nvs_read_string(handle, "sage_secret", sage_secret, sizeof(sage_secret));
    nvs_close(handle);
    return err;
}

static esp_err_t save_settings(void)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open("wearable", NVS_READWRITE, &handle);
    if (err != ESP_OK) return err;
    err = nvs_set_str(handle, "wifi_ssid", wifi_ssid);
    if (err == ESP_OK) err = nvs_set_str(handle, "wifi_password", wifi_password);
    if (err == ESP_OK) err = nvs_set_str(handle, "sage_host", sage_host);
    if (err == ESP_OK) err = nvs_set_str(handle, "sage_secret", sage_secret);
    if (err == ESP_OK) err = nvs_commit(handle);
    nvs_close(handle);
    return err;
}

static esp_err_t oled_command(uint8_t command)
{
    uint8_t data[] = {0x00, command};
    return i2c_master_write_to_device(I2C_PORT, CONFIG_SAGE_WEARABLE_OLED_ADDRESS,
                                      data, sizeof(data), pdMS_TO_TICKS(100));
}

static void oled_init(void)
{
    i2c_config_t config = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = CONFIG_SAGE_WEARABLE_OLED_SDA,
        .scl_io_num = CONFIG_SAGE_WEARABLE_OLED_SCL,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = 400000,
    };
    ESP_ERROR_CHECK(i2c_param_config(I2C_PORT, &config));
    ESP_ERROR_CHECK(i2c_driver_install(I2C_PORT, config.mode, 0, 0, 0));
    const uint8_t commands[] = {
        0xae, 0xd5, 0x80, 0xa8, 0x3f, 0xd3, 0x00, 0x40, 0x8d, 0x14,
        0x20, 0x00, 0xa1, 0xc8, 0xda, 0x12, 0x81, 0x7f, 0xd9, 0xf1,
        0xdb, 0x40, 0xa4, 0xa6, 0xaf,
    };
    for (size_t i = 0; i < sizeof(commands); ++i) {
        if (oled_command(commands[i]) != ESP_OK) {
            ESP_LOGW(TAG, "OLED not detected at 0x%02x", CONFIG_SAGE_WEARABLE_OLED_ADDRESS);
            return;
        }
    }
}

static void oled_status(const char *status)
{
    ESP_LOGI(TAG, "OLED: %s", status);
    /* Text rendering is added after the controller is identified. */
}

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(wifi_events, WIFI_CONNECTED);
        esp_wifi_connect();
        oled_status("Wi-Fi reconnecting");
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(wifi_events, WIFI_CONNECTED);
        oled_status("Wi-Fi connected");
    }
}

static void start_station(void)
{
    wifi_events = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_create_default_wifi_sta() ? ESP_OK : ESP_FAIL);
    wifi_init_config_t wifi_config = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&wifi_config));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL));
    wifi_config_t station = {0};
    strlcpy((char *)station.sta.ssid, wifi_ssid, sizeof(station.sta.ssid));
    strlcpy((char *)station.sta.password, wifi_password, sizeof(station.sta.password));
    station.sta.threshold.authmode = WIFI_AUTH_OPEN;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &station));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_connect());
}

static esp_err_t setup_get(httpd_req_t *request)
{
    const char *page =
        "<!doctype html><meta name='viewport' content='width=device-width'>"
        "<title>Sage wearable setup</title><h1>Sage wearable</h1>"
        "<form method='post'><label>Wi-Fi SSID <input name='ssid' required></label><br>"
        "<label>Wi-Fi password <input name='password' type='password'></label><br>"
        "<label>Sage host <input name='host' placeholder='192.168.1.20' required></label><br>"
        "<label>Voice secret <input name='secret' type='password' required></label><br>"
        "<button>Save and reboot</button></form>";
    httpd_resp_set_type(request, "text/html; charset=utf-8");
    return httpd_resp_send(request, page, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t setup_post(httpd_req_t *request)
{
    char body[512] = {0};
    int length = httpd_req_recv(request, body, sizeof(body) - 1);
    if (length <= 0) return ESP_FAIL;
    body[length] = '\0';
    char *fields[] = {wifi_ssid, wifi_password, sage_host, sage_secret};
    const char *keys[] = {"ssid", "password", "host", "secret"};
    for (size_t i = 0; i < 4; ++i) {
        char key[24];
        snprintf(key, sizeof(key), "%s=", keys[i]);
        char *value = strstr(body, key);
        if (!value) continue;
        value += strlen(key);
        char *end = strchr(value, '&');
        if (end) *end = '\0';
        strlcpy(fields[i], value, i == 1 ? sizeof(wifi_password) :
                 i == 3 ? sizeof(sage_secret) :
                 i == 2 ? sizeof(sage_host) : sizeof(wifi_ssid));
    }
    if (!wifi_ssid[0] || !sage_host[0] || !sage_secret[0] || save_settings() != ESP_OK) {
        httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "Invalid setup values");
        return ESP_OK;
    }
    httpd_resp_sendstr(request, "Saved. Rebooting; reconnect to your normal Wi-Fi.");
    vTaskDelay(pdMS_TO_TICKS(500));
    esp_restart();
    return ESP_OK;
}

static void start_setup_ap(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    ESP_ERROR_CHECK(esp_netif_create_default_wifi_ap() ? ESP_OK : ESP_FAIL);
    wifi_init_config_t config = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&config));
    wifi_config_t ap = {0};
    strlcpy((char *)ap.ap.ssid, "Sage-Wearable-Setup", sizeof(ap.ap.ssid));
    strlcpy((char *)ap.ap.password, CONFIG_SAGE_WEARABLE_AP_PASSWORD, sizeof(ap.ap.password));
    ap.ap.authmode = WIFI_AUTH_WPA2_PSK;
    ap.ap.max_connection = 2;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap));
    ESP_ERROR_CHECK(esp_wifi_start());
    httpd_config_t server_config = HTTPD_DEFAULT_CONFIG();
    ESP_ERROR_CHECK(httpd_start(&setup_server, &server_config));
    httpd_uri_t get = {.uri = "/", .method = HTTP_GET, .handler = setup_get};
    httpd_uri_t post = {.uri = "/", .method = HTTP_POST, .handler = setup_post};
    ESP_ERROR_CHECK(httpd_register_uri_handler(setup_server, &get));
    ESP_ERROR_CHECK(httpd_register_uri_handler(setup_server, &post));
    oled_status("Setup: 192.168.4.1");
    ESP_LOGI(TAG, "Setup AP Sage-Wearable-Setup; open http://192.168.4.1/");
}

static void button_task(void *arg)
{
    gpio_config_t button = {
        .pin_bit_mask = 1ULL << CONFIG_SAGE_WEARABLE_BUTTON_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&button));
    bool previous = false;
    for (;;) {
        bool pressed = gpio_get_level(CONFIG_SAGE_WEARABLE_BUTTON_GPIO) == 0;
        if (pressed != previous) {
            previous = pressed;
            listening = pressed;
            oled_status(pressed ? "Listening" : "Ready");
            ESP_LOGI(TAG, "Push-to-talk %s", pressed ? "started" : "stopped");
        }
        vTaskDelay(pdMS_TO_TICKS(30));
    }
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    ESP_ERROR_CHECK(load_settings());
    oled_init();
    xTaskCreate(button_task, "button", 3072, NULL, 5, NULL);
    if (wifi_ssid[0] && wifi_password[0] && sage_host[0] && sage_secret[0]) {
        start_station();
        oled_status("Ready");
    } else {
        setup_mode = true;
        start_setup_ap();
    }
}

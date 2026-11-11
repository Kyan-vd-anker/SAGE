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
#include "bt_audio.h"

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

static void start_setup_server(void);

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

static const uint8_t *oled_glyph(char character)
{
    static const uint8_t blank[5] = {0, 0, 0, 0, 0};
    static const uint8_t digits[10][5] = {
        {0x3e, 0x51, 0x49, 0x45, 0x3e}, {0x00, 0x42, 0x7f, 0x40, 0x00},
        {0x42, 0x61, 0x51, 0x49, 0x46}, {0x21, 0x41, 0x45, 0x4b, 0x31},
        {0x18, 0x14, 0x12, 0x7f, 0x10}, {0x27, 0x45, 0x45, 0x45, 0x39},
        {0x3c, 0x4a, 0x49, 0x49, 0x30}, {0x01, 0x71, 0x09, 0x05, 0x03},
        {0x36, 0x49, 0x49, 0x49, 0x36}, {0x06, 0x49, 0x49, 0x29, 0x1e},
    };
    static const uint8_t letters[26][5] = {
        {0x7e, 0x11, 0x11, 0x11, 0x7e}, {0x7f, 0x49, 0x49, 0x49, 0x36},
        {0x3e, 0x41, 0x41, 0x41, 0x22}, {0x7f, 0x41, 0x41, 0x22, 0x1c},
        {0x7f, 0x49, 0x49, 0x49, 0x41}, {0x7f, 0x09, 0x09, 0x09, 0x01},
        {0x3e, 0x41, 0x49, 0x49, 0x7a}, {0x7f, 0x08, 0x08, 0x08, 0x7f},
        {0x00, 0x41, 0x7f, 0x41, 0x00}, {0x20, 0x40, 0x41, 0x3f, 0x01},
        {0x7f, 0x08, 0x14, 0x22, 0x41}, {0x7f, 0x40, 0x40, 0x40, 0x40},
        {0x7f, 0x02, 0x0c, 0x02, 0x7f}, {0x7f, 0x04, 0x08, 0x10, 0x7f},
        {0x3e, 0x41, 0x41, 0x41, 0x3e}, {0x7f, 0x09, 0x09, 0x09, 0x06},
        {0x3e, 0x41, 0x51, 0x21, 0x5e}, {0x7f, 0x09, 0x19, 0x29, 0x46},
        {0x46, 0x49, 0x49, 0x49, 0x31}, {0x01, 0x01, 0x7f, 0x01, 0x01},
        {0x3f, 0x40, 0x40, 0x40, 0x3f}, {0x1f, 0x20, 0x40, 0x20, 0x1f},
        {0x3f, 0x40, 0x38, 0x40, 0x3f}, {0x63, 0x14, 0x08, 0x14, 0x63},
        {0x07, 0x08, 0x70, 0x08, 0x07}, {0x61, 0x51, 0x49, 0x45, 0x43},
    };
    static const uint8_t colon[5] = {0x00, 0x36, 0x36, 0x00, 0x00};
    static const uint8_t dash[5] = {0x08, 0x08, 0x08, 0x08, 0x08};
    static const uint8_t dot[5] = {0x00, 0x60, 0x60, 0x00, 0x00};
    static const uint8_t slash[5] = {0x20, 0x10, 0x08, 0x04, 0x02};

    if (character >= 'a' && character <= 'z') character -= 'a' - 'A';
    if (character >= 'A' && character <= 'Z') return letters[character - 'A'];
    if (character >= '0' && character <= '9') return digits[character - '0'];
    if (character == ':') return colon;
    if (character == '-') return dash;
    if (character == '.') return dot;
    if (character == '/') return slash;
    return blank;
}

static void oled_clear(void)
{
    uint8_t buffer[OLED_WIDTH + 1] = {0};
    buffer[0] = 0x40;
    for (uint8_t page = 0; page < OLED_PAGE_COUNT; ++page) {
        oled_command(0xb0 | page);
        oled_command(0x02);
        oled_command(0x10);
        i2c_master_write_to_device(I2C_PORT, CONFIG_SAGE_WEARABLE_OLED_ADDRESS,
                                   buffer, sizeof(buffer), pdMS_TO_TICKS(100));
    }
}

static void oled_text(const char *text)
{
    uint8_t buffer[OLED_WIDTH + 1] = {0};
    buffer[0] = 0x40;
    size_t column = 0;
    for (; *text && column + 6 <= OLED_WIDTH; ++text) {
        const uint8_t *glyph = oled_glyph(*text);
        for (size_t index = 0; index < 5; ++index) {
            buffer[column + 1 + index] = glyph[index];
        }
        column += 6;
    }
    oled_command(0xb0);
    oled_command(0x02);
    oled_command(0x10);
    i2c_master_write_to_device(I2C_PORT, CONFIG_SAGE_WEARABLE_OLED_ADDRESS,
                               buffer, sizeof(buffer), pdMS_TO_TICKS(100));
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
    oled_clear();
}

static void oled_status(const char *status)
{
    ESP_LOGI(TAG, "OLED: %s", status);
    oled_text(status);
}

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(wifi_events, WIFI_CONNECTED);
        esp_wifi_connect();
        oled_status("Wi-Fi reconnecting");
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(wifi_events, WIFI_CONNECTED);
        const ip_event_got_ip_t *event = (const ip_event_got_ip_t *)data;
        oled_status("Wi-Fi connected");
        ESP_LOGI(TAG, "Web UI available at http://" IPSTR "/", IP2STR(&event->ip_info.ip));
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
    start_setup_server();
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
        "<button>Save and reboot</button></form>"
        "<hr><h2>AirPods</h2><p id='bt'>Checking Bluetooth...</p>"
        "<button type='button' onclick='scanBluetooth()'>Scan Bluetooth devices</button>"
        "<ul id='devices'></ul>"
        "<script>"
        "async function bluetoothStatus(){"
        "const response=await fetch('/bluetooth/status');"
        "document.getElementById('bt').textContent=await response.text();"
        "const devices=await (await fetch('/bluetooth/devices')).json();"
        "document.getElementById('devices').innerHTML=devices.devices.map(d=>"
        "`<li>${d.name} (${d.address}, ${d.rssi} dBm) "
        "<button type='button' onclick=\"connectBluetooth('${d.address}')\">Connect</button></li>`)"
        ".join('')||'<li>No devices found</li>';"
        "}"
        "async function connectBluetooth(address){"
        "const body=new URLSearchParams({address});"
        "const response=await fetch('/bluetooth/connect',{method:'POST',"
        "headers:{'Content-Type':'application/x-www-form-urlencoded'},body});"
        "document.getElementById('bt').textContent=await response.text();"
        "}"
        "async function scanBluetooth(){"
        "document.getElementById('bt').textContent='Starting Bluetooth scan...';"
        "await fetch('/bluetooth/scan',{method:'POST'}); bluetoothStatus();"
        "}"
        "bluetoothStatus();setInterval(bluetoothStatus,2000);"
        "</script>";
    httpd_resp_set_type(request, "text/html; charset=utf-8");
    return httpd_resp_send(request, page, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t bluetooth_devices_get(httpd_req_t *request)
{
    char devices[4096];
    bt_audio_get_devices_json(devices, sizeof(devices));
    httpd_resp_set_type(request, "application/json");
    return httpd_resp_send(request, devices, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t bluetooth_status_get(httpd_req_t *request)
{
    char status[96];
    bt_audio_get_status(status, sizeof(status));
    httpd_resp_set_type(request, "text/plain; charset=utf-8");
    return httpd_resp_send(request, status, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t bluetooth_scan_post(httpd_req_t *request)
{
    bt_audio_scan();
    return httpd_resp_sendstr(request, "Bluetooth scan started");
}

static int hex_value(char character)
{
    if (character >= '0' && character <= '9') return character - '0';
    if (character >= 'a' && character <= 'f') return character - 'a' + 10;
    if (character >= 'A' && character <= 'F') return character - 'A' + 10;
    return -1;
}

static bool url_decode(const char *encoded, char *decoded, size_t decoded_size)
{
    size_t output = 0;
    for (size_t input = 0; encoded[input]; ++input) {
        if (output + 1 >= decoded_size) return false;
        if (encoded[input] == '%') {
            if (!encoded[input + 1] || !encoded[input + 2]) return false;
            int high = hex_value(encoded[input + 1]);
            int low = hex_value(encoded[input + 2]);
            if (high < 0 || low < 0) return false;
            decoded[output++] = (char)((high << 4) | low);
            input += 2;
        } else if (encoded[input] == '+') {
            decoded[output++] = ' ';
        } else {
            decoded[output++] = encoded[input];
        }
    }
    decoded[output] = '\0';
    return true;
}

static esp_err_t bluetooth_connect_post(httpd_req_t *request)
{
    char body[64] = {0};
    if (request->content_len == 0 || request->content_len >= sizeof(body)) {
        httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "Missing Bluetooth address");
        return ESP_OK;
    }
    size_t received = 0;
    while (received < request->content_len) {
        int length = httpd_req_recv(request, body + received,
                                    request->content_len - received);
        if (length <= 0) {
            httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST,
                                "Could not read Bluetooth address");
            return ESP_OK;
        }
        received += (size_t)length;
    }
    body[received] = '\0';
    char encoded_address[32] = {0};
    char address[18] = {0};
    if (httpd_query_key_value(body, "address", encoded_address,
                              sizeof(encoded_address)) != ESP_OK ||
        !url_decode(encoded_address, address, sizeof(address)) ||
        !bt_audio_connect_device(address)) {
        httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "Invalid Bluetooth address");
        return ESP_OK;
    }
    return httpd_resp_sendstr(request, "Connecting Bluetooth profiles");
}

static esp_err_t setup_post(httpd_req_t *request)
{
    char body[512] = {0};
    if (request->content_len >= sizeof(body)) {
        httpd_resp_send_err(request, HTTPD_413_CONTENT_TOO_LARGE, "Setup values are too long");
        return ESP_OK;
    }

    size_t received = 0;
    while (received < request->content_len) {
        int length = httpd_req_recv(request, body + received,
                                    request->content_len - received);
        if (length <= 0) {
            ESP_LOGW(TAG, "Could not read setup request body: %d", length);
            httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "Could not read setup values");
            return ESP_OK;
        }
        received += (size_t)length;
    }
    body[received] = '\0';

    char new_wifi_ssid[sizeof(wifi_ssid)] = {0};
    char new_wifi_password[sizeof(wifi_password)] = {0};
    char new_sage_host[sizeof(sage_host)] = {0};
    char new_sage_secret[sizeof(sage_secret)] = {0};
    esp_err_t err = httpd_query_key_value(body, "ssid", new_wifi_ssid,
                                           sizeof(new_wifi_ssid));
    if (err == ESP_OK) {
        err = httpd_query_key_value(body, "password", new_wifi_password,
                                    sizeof(new_wifi_password));
        if (err == ESP_ERR_NOT_FOUND) {
            err = ESP_OK;
        }
    }
    if (err == ESP_OK) {
        err = httpd_query_key_value(body, "host", new_sage_host,
                                    sizeof(new_sage_host));
    }
    if (err == ESP_OK) {
        err = httpd_query_key_value(body, "secret", new_sage_secret,
                                    sizeof(new_sage_secret));
    }
    if (err != ESP_OK || !new_wifi_ssid[0] || !new_sage_host[0] || !new_sage_secret[0]) {
        ESP_LOGW(TAG, "Invalid setup values: missing or oversized field (error %s)",
                 esp_err_to_name(err));
        httpd_resp_send_err(request, HTTPD_400_BAD_REQUEST, "Invalid setup values");
        return ESP_OK;
    }

    strlcpy(wifi_ssid, new_wifi_ssid, sizeof(wifi_ssid));
    strlcpy(wifi_password, new_wifi_password, sizeof(wifi_password));
    strlcpy(sage_host, new_sage_host, sizeof(sage_host));
    strlcpy(sage_secret, new_sage_secret, sizeof(sage_secret));
    err = save_settings();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Could not save setup values: %s", esp_err_to_name(err));
        httpd_resp_send_err(request, HTTPD_500_INTERNAL_SERVER_ERROR,
                            "Could not save setup values");
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
    start_setup_server();
    oled_status("Setup: 192.168.4.1");
    ESP_LOGI(TAG, "Setup AP Sage-Wearable-Setup; open http://192.168.4.1/");
}

static void start_setup_server(void)
{
    httpd_config_t server_config = HTTPD_DEFAULT_CONFIG();
    ESP_ERROR_CHECK(httpd_start(&setup_server, &server_config));
    httpd_uri_t get = {.uri = "/", .method = HTTP_GET, .handler = setup_get};
    httpd_uri_t post = {.uri = "/", .method = HTTP_POST, .handler = setup_post};
    httpd_uri_t bluetooth_status = {
        .uri = "/bluetooth/status", .method = HTTP_GET, .handler = bluetooth_status_get
    };
    httpd_uri_t bluetooth_scan = {
        .uri = "/bluetooth/scan", .method = HTTP_POST, .handler = bluetooth_scan_post
    };
    httpd_uri_t bluetooth_devices = {
        .uri = "/bluetooth/devices", .method = HTTP_GET, .handler = bluetooth_devices_get
    };
    httpd_uri_t bluetooth_connect = {
        .uri = "/bluetooth/connect", .method = HTTP_POST, .handler = bluetooth_connect_post
    };
    ESP_ERROR_CHECK(httpd_register_uri_handler(setup_server, &get));
    ESP_ERROR_CHECK(httpd_register_uri_handler(setup_server, &post));
    ESP_ERROR_CHECK(httpd_register_uri_handler(setup_server, &bluetooth_status));
    ESP_ERROR_CHECK(httpd_register_uri_handler(setup_server, &bluetooth_scan));
    ESP_ERROR_CHECK(httpd_register_uri_handler(setup_server, &bluetooth_devices));
    ESP_ERROR_CHECK(httpd_register_uri_handler(setup_server, &bluetooth_connect));
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
            bt_audio_set_push_to_talk(pressed);
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
    bt_audio_start();
    xTaskCreate(button_task, "button", 3072, NULL, 5, NULL);
    if (wifi_ssid[0] && sage_host[0] && sage_secret[0]) {
        start_station();
        oled_status("Ready");
    } else {
        setup_mode = true;
        start_setup_ap();
    }
}

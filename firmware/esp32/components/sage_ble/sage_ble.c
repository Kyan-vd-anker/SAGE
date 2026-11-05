#include <stdbool.h>
#include <stdint.h>

#include "sage_ble.h"
#include "sdkconfig.h"

#if CONFIG_BT_ENABLED && CONFIG_BT_BLUEDROID_ENABLED && CONFIG_BT_CONTROLLER_ENABLED && CONFIG_BT_BLE_ENABLED
#include "esp_bt.h"
#include "esp_bt_main.h"
#include "esp_gap_ble_api.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/portmacro.h"

#define NO_SIGHTING (-128)

static const char *TAG = "sage-ble";
static portMUX_TYPE sighting_lock = portMUX_INITIALIZER_UNLOCKED;
static int latest_rssi = NO_SIGHTING;
static int64_t latest_sighting_us;

static bool has_service_uuid(const uint8_t *data, uint8_t length)
{
    for (size_t offset = 0; offset < length;) {
        uint8_t field_length = data[offset];
        if (field_length == 0) {
            break;
        }
        if (offset + field_length >= length) {
            return false;
        }
        uint8_t type = data[offset + 1];
        if ((type == 0x02 || type == 0x03) && field_length >= 3) {
            for (size_t index = 2; index + 1 <= field_length; index += 2) {
                uint16_t uuid = data[offset + index] |
                                ((uint16_t)data[offset + index + 1] << 8);
                if (uuid == CONFIG_SAGE_BLE_SERVICE_UUID) {
                    return true;
                }
            }
        }
        offset += field_length + 1;
    }
    return false;
}

static void gap_callback(esp_gap_ble_cb_event_t event, esp_ble_gap_cb_param_t *param)
{
    if (event == ESP_GAP_BLE_SCAN_PARAM_SET_COMPLETE_EVT) {
        if (param->scan_param_cmpl.status != ESP_BT_STATUS_SUCCESS) {
            ESP_LOGE(TAG, "BLE scan parameter setup failed: %d", param->scan_param_cmpl.status);
            return;
        }
        esp_err_t err = esp_ble_gap_start_scanning(0);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Could not start BLE scan: %s", esp_err_to_name(err));
        }
    } else if (event == ESP_GAP_BLE_SCAN_RESULT_EVT &&
               param->scan_rst.search_evt == ESP_GAP_SEARCH_INQ_RES_EVT &&
               has_service_uuid(param->scan_rst.ble_adv, param->scan_rst.adv_data_len)) {
        portENTER_CRITICAL(&sighting_lock);
        latest_rssi = param->scan_rst.rssi;
        latest_sighting_us = esp_timer_get_time();
        portEXIT_CRITICAL(&sighting_lock);
    } else if (event == ESP_GAP_BLE_ADV_DATA_RAW_SET_COMPLETE_EVT &&
               param->adv_data_raw_cmpl.status == ESP_BT_STATUS_SUCCESS) {
        esp_ble_adv_params_t params = {
            .adv_int_min = 0x20,
            .adv_int_max = 0x40,
            .adv_type = ADV_TYPE_NONCONN_IND,
            .own_addr_type = BLE_ADDR_TYPE_PUBLIC,
            .channel_map = ADV_CHNL_ALL,
            .adv_filter_policy = ADV_FILTER_ALLOW_SCAN_ANY_CON_ANY,
        };
        esp_err_t err = esp_ble_gap_start_advertising(&params);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Could not start BLE beacon: %s", esp_err_to_name(err));
        }
    }
}

static esp_err_t start_bluetooth(void)
{
    esp_err_t err;
#if CONFIG_IDF_TARGET_ESP32S3
    err = esp_bt_controller_mem_release(ESP_BT_MODE_CLASSIC_BT);
    if (err != ESP_OK) {
        return err;
    }
#endif
    esp_bt_controller_config_t config = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    err = esp_bt_controller_init(&config);
    if (err != ESP_OK) {
        return err;
    }
    err = esp_bt_controller_enable(ESP_BT_MODE_BLE);
    if (err != ESP_OK) {
        return err;
    }
    err = esp_bluedroid_init();
    if (err != ESP_OK) {
        return err;
    }
    err = esp_bluedroid_enable();
    if (err != ESP_OK) {
        return err;
    }
    return esp_ble_gap_register_callback(gap_callback);
}

esp_err_t sage_ble_start_scanner(void)
{
    esp_err_t err = start_bluetooth();
    if (err != ESP_OK) {
        return err;
    }
    esp_ble_scan_params_t params = {
        .scan_type = BLE_SCAN_TYPE_ACTIVE,
        .own_addr_type = BLE_ADDR_TYPE_PUBLIC,
        .scan_filter_policy = BLE_SCAN_FILTER_ALLOW_ALL,
        .scan_interval = 0x50,
        .scan_window = 0x30,
        .scan_duplicate = BLE_SCAN_DUPLICATE_DISABLE,
    };
    return esp_ble_gap_set_scan_params(&params);
}

esp_err_t sage_ble_start_beacon(void)
{
    esp_err_t err = start_bluetooth();
    if (err != ESP_OK) {
        return err;
    }
    uint8_t advertisement[] = {
        0x02, 0x01, 0x06,
        0x03, 0x03,
        (uint8_t)(CONFIG_SAGE_BLE_SERVICE_UUID & 0xff),
        (uint8_t)(CONFIG_SAGE_BLE_SERVICE_UUID >> 8),
    };
    return esp_ble_gap_config_adv_data_raw(advertisement, sizeof(advertisement));
}

int sage_ble_last_rssi(int64_t max_age_ms)
{
    int rssi = NO_SIGHTING;
    int64_t now = esp_timer_get_time();
    portENTER_CRITICAL(&sighting_lock);
    if (latest_sighting_us && now >= latest_sighting_us &&
        now - latest_sighting_us <= max_age_ms * 1000) {
        rssi = latest_rssi;
    }
    portEXIT_CRITICAL(&sighting_lock);
    return rssi;
}
#else
esp_err_t sage_ble_start_scanner(void)
{
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t sage_ble_start_beacon(void)
{
    return ESP_ERR_NOT_SUPPORTED;
}

int sage_ble_last_rssi(int64_t max_age_ms)
{
    return -128;
}
#endif

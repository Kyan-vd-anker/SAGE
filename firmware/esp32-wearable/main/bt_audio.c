#include "bt_audio.h"

#include <stdio.h>
#include <string.h>

#include "esp_a2dp_api.h"
#include "esp_avrc_api.h"
#include "esp_bt.h"
#include "esp_bt_device.h"
#include "esp_bt_main.h"
#include "esp_gap_bt_api.h"
#include "esp_hf_ag_api.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

/* Set to 0 to test voice/mic alone: the headset then sees the ESP32 purely as a call device.
 * AVRCP play/pause (the push-to-talk button on the headset) normally rides on the A2DP link,
 * so it may stop arriving while this is 0. */
#ifndef SAGE_BT_A2DP
#define SAGE_BT_A2DP 1
#endif

#define MAX_DISCOVERED_DEVICES 24
#define DEVICE_NAME_SIZE 64

static const char *TAG = "sage-bt";
static SemaphoreHandle_t state_lock;
static bool push_to_talk;
static esp_bd_addr_t airpods_address;
static bool airpods_found;
static bool a2dp_connected;
static bool hfp_connected;
static bool scanning;
static char airpods_name[ESP_BT_GAP_MAX_BDNAME_LEN + 1];
typedef struct {
    esp_bd_addr_t address;
    char name[DEVICE_NAME_SIZE];
    int rssi;
} discovered_device_t;
static discovered_device_t devices[MAX_DISCOVERED_DEVICES];
static size_t device_count;

static void set_push_to_talk(bool active)
{
    xSemaphoreTake(state_lock, portMAX_DELAY);
    push_to_talk = active;
    xSemaphoreGive(state_lock);
    ESP_LOGI(TAG, "push-to-talk %s", active ? "started" : "stopped");
}

static void set_status_state(void)
{
    if (scanning) {
        return;
    }
    if (a2dp_connected && hfp_connected) {
        strcpy(airpods_name, "connected");
    } else if (airpods_found) {
        strcpy(airpods_name, "found; connecting");
    } else {
        strcpy(airpods_name, "not connected");
    }
}

void bt_audio_get_status(char *status, size_t status_size)
{
    xSemaphoreTake(state_lock, portMAX_DELAY);
    if (scanning) {
        snprintf(status, status_size, "scanning for AirPods");
    } else if (airpods_found && airpods_name[0]) {
        snprintf(status, status_size, "%s", airpods_name);
    } else {
        snprintf(status, status_size, "not connected");
    }
    xSemaphoreGive(state_lock);
}

void bt_audio_scan(void)
{
    xSemaphoreTake(state_lock, portMAX_DELAY);
    if (scanning) {
        xSemaphoreGive(state_lock);
        return;
    }
    airpods_found = false;
    a2dp_connected = false;
    hfp_connected = false;
    scanning = true;
    airpods_name[0] = '\0';
    device_count = 0;
    xSemaphoreGive(state_lock);
    esp_err_t err = esp_bt_gap_start_discovery(ESP_BT_INQ_MODE_GENERAL_INQUIRY, 10, 0);
    if (err != ESP_OK) {
        xSemaphoreTake(state_lock, portMAX_DELAY);
        scanning = false;
        xSemaphoreGive(state_lock);
        ESP_LOGE(TAG, "could not start Bluetooth discovery: %s", esp_err_to_name(err));
    }
}

static void format_address(const esp_bd_addr_t address, char *output, size_t output_size)
{
    snprintf(output, output_size, "%02x:%02x:%02x:%02x:%02x:%02x",
             address[0], address[1], address[2], address[3], address[4], address[5]);
}

void bt_audio_get_devices_json(char *json, size_t json_size)
{
    size_t used = 0;
    int written;
    xSemaphoreTake(state_lock, portMAX_DELAY);
    written = snprintf(json, json_size, "{\"scanning\":%s,\"devices\":[",
                       scanning ? "true" : "false");
    if (written < 0 || (size_t)written >= json_size) {
        xSemaphoreGive(state_lock);
        return;
    }
    used = (size_t)written;
    for (size_t index = 0; index < device_count; ++index) {
        char address[18];
        format_address(devices[index].address, address, sizeof(address));
        written = snprintf(json + used, json_size - used,
                           "%s{\"address\":\"%s\",\"name\":\"",
                           index ? "," : "", address);
        if (written < 0 || (size_t)written >= json_size - used) {
            break;
        }
        used += (size_t)written;
        for (const char *name = devices[index].name; *name; ++name) {
            const char *replacement = NULL;
            if (*name == '"' || *name == '\\') {
                replacement = (*name == '"') ? "\\\"" : "\\\\";
            } else if ((unsigned char)*name < 0x20) {
                replacement = " ";
            }
            written = snprintf(json + used, json_size - used, "%s",
                               replacement ? replacement : (char[]){*name, '\0'});
            if (written < 0 || (size_t)written >= json_size - used) {
                break;
            }
            used += (size_t)written;
        }
        written = snprintf(json + used, json_size - used, "\",\"rssi\":%d}",
                           devices[index].rssi);
        if (written < 0 || (size_t)written >= json_size - used) {
            break;
        }
        used += (size_t)written;
    }
    snprintf(json + used, json_size - used, "]}");
    xSemaphoreGive(state_lock);
}

bool bt_audio_connect_device(const char *address)
{
    unsigned int octets[ESP_BD_ADDR_LEN];
    if (sscanf(address, "%2x:%2x:%2x:%2x:%2x:%2x",
               &octets[0], &octets[1], &octets[2],
               &octets[3], &octets[4], &octets[5]) != ESP_BD_ADDR_LEN) {
        return false;
    }
    esp_bd_addr_t device_address;
    for (size_t index = 0; index < ESP_BD_ADDR_LEN; ++index) {
        if (octets[index] > 0xff) {
            return false;
        }
        device_address[index] = (uint8_t)octets[index];
    }
    xSemaphoreTake(state_lock, portMAX_DELAY);
    memcpy(airpods_address, device_address, sizeof(airpods_address));
    airpods_found = true;
    scanning = false;
    snprintf(airpods_name, sizeof(airpods_name), "selected; connecting");
    xSemaphoreGive(state_lock);
    esp_err_t a2dp_err = ESP_OK;
#if SAGE_BT_A2DP
    a2dp_err = esp_a2d_source_connect(device_address);
#endif
    esp_err_t hfp_err = esp_hf_ag_slc_connect(device_address);
    if (a2dp_err != ESP_OK || hfp_err != ESP_OK) {
        ESP_LOGE(TAG, "Bluetooth connect failed: A2DP=%s HFP=%s",
                 esp_err_to_name(a2dp_err), esp_err_to_name(hfp_err));
        return false;
    }
    ESP_LOGI(TAG, "connecting Bluetooth profiles to %s", address);
    return true;
}

bool bt_audio_is_push_to_talk(void)
{
    bool active;
    xSemaphoreTake(state_lock, portMAX_DELAY);
    active = push_to_talk;
    xSemaphoreGive(state_lock);
    return active;
}

void bt_audio_set_push_to_talk(bool active)
{
    set_push_to_talk(active);
}

static int32_t a2dp_source_data(uint8_t *buffer, int32_t length)
{
    memset(buffer, 0, (size_t)length);
    return length;
}

static void a2dp_callback(esp_a2d_cb_event_t event, esp_a2d_cb_param_t *param)
{
    if (event == ESP_A2D_CONNECTION_STATE_EVT) {
        xSemaphoreTake(state_lock, portMAX_DELAY);
        a2dp_connected = param->conn_stat.state == ESP_A2D_CONNECTION_STATE_CONNECTED;
        set_status_state();
        xSemaphoreGive(state_lock);
        ESP_LOGI(TAG, "A2DP state %d", param->conn_stat.state);
    } else if (event == ESP_A2D_AUDIO_STATE_EVT) {
        ESP_LOGI(TAG, "A2DP audio state %d", param->audio_stat.state);
    }
}

static void hfp_incoming_data(const uint8_t *buffer, uint32_t length)
{
    if (bt_audio_is_push_to_talk()) {
        ESP_LOGD(TAG, "received %lu HFP microphone bytes", (unsigned long)length);
    }
    (void)buffer;
}

static uint32_t hfp_outgoing_data(uint8_t *buffer, uint32_t length)
{
    memset(buffer, 0, length);
    return length;
}

static void hfp_callback(esp_hf_cb_event_t event, esp_hf_cb_param_t *param)
{
    if (event == ESP_HF_CONNECTION_STATE_EVT) {
        xSemaphoreTake(state_lock, portMAX_DELAY);
        hfp_connected = param->conn_stat.state >= ESP_HF_CONNECTION_STATE_CONNECTED;
        set_status_state();
        xSemaphoreGive(state_lock);
        ESP_LOGI(TAG, "HFP state %d", param->conn_stat.state);
        if (param->conn_stat.state == ESP_HF_CONNECTION_STATE_SLC_CONNECTED) {
            /* The service-level connection is up: open the SCO voice/mic link ourselves.
             * The headset will not do it for an audio gateway that stays silent. */
            esp_err_t err = esp_hf_ag_audio_connect(param->conn_stat.remote_bda);
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "could not open the SCO audio link: %s", esp_err_to_name(err));
            }
        }
    } else if (event == ESP_HF_AUDIO_STATE_EVT) {
        ESP_LOGI(TAG, "HFP audio state %d (%s)", param->audio_stat.state,
                 param->audio_stat.state == ESP_HF_AUDIO_STATE_CONNECTED_MSBC ? "mSBC wideband, 16 kHz" :
                 param->audio_stat.state == ESP_HF_AUDIO_STATE_CONNECTED ? "CVSD narrowband, 8 kHz" : "other");
        if (param->audio_stat.state == ESP_HF_AUDIO_STATE_DISCONNECTED) {
            set_push_to_talk(false);
        }
    } else if (event == ESP_HF_BVRA_RESPONSE_EVT) {
        set_push_to_talk(param->vra_rep.value == ESP_HF_VR_STATE_ENABLED);
    }
}

static void avrc_target_callback(esp_avrc_tg_cb_event_t event,
                                 esp_avrc_tg_cb_param_t *param)
{
    if (event != ESP_AVRC_TG_PASSTHROUGH_CMD_EVT ||
        param->psth_cmd.key_state != ESP_AVRC_PT_CMD_STATE_PRESSED) {
        return;
    }
    if (param->psth_cmd.key_code == ESP_AVRC_PT_CMD_PLAY ||
        param->psth_cmd.key_code == ESP_AVRC_PT_CMD_PAUSE) {
        set_push_to_talk(!bt_audio_is_push_to_talk());
    }
}

static void gap_callback(esp_bt_gap_cb_event_t event, esp_bt_gap_cb_param_t *param)
{
    if (event == ESP_BT_GAP_PIN_REQ_EVT) {
        esp_bt_pin_code_t pin = {'0', '0', '0', '0'};
        esp_bt_gap_pin_reply(param->pin_req.bda, true, 4, pin);
    } else if (event == ESP_BT_GAP_DISC_RES_EVT) {
        char name[DEVICE_NAME_SIZE] = "Unknown";
        int rssi = -128;
        bool has_name = false;
        for (int index = 0; index < param->disc_res.num_prop; ++index) {
            esp_bt_gap_dev_prop_t *property = &param->disc_res.prop[index];
            if (property->type == ESP_BT_GAP_DEV_PROP_BDNAME) {
                size_t length = (size_t)property->len;
                if (length >= sizeof(name)) {
                    length = sizeof(name) - 1;
                }
                memcpy(name, property->val, length);
                name[length] = '\0';
                has_name = true;
            } else if (property->type == ESP_BT_GAP_DEV_PROP_RSSI &&
                       property->len >= sizeof(int8_t)) {
                rssi = *(int8_t *)property->val;
            }
        }
        xSemaphoreTake(state_lock, portMAX_DELAY);
        size_t device_index = 0;
        for (; device_index < device_count; ++device_index) {
            if (memcmp(devices[device_index].address, param->disc_res.bda,
                       ESP_BD_ADDR_LEN) == 0) {
                break;
            }
        }
        if (device_index == device_count && device_count < MAX_DISCOVERED_DEVICES) {
            memcpy(devices[device_count].address, param->disc_res.bda, ESP_BD_ADDR_LEN);
            device_index = device_count++;
        }
        if (device_index < device_count) {
            if (has_name) {
                strlcpy(devices[device_index].name, name, sizeof(devices[device_index].name));
            }
            devices[device_index].rssi = rssi;
        }
        if (has_name && strstr(name, "AirPods") != NULL && !airpods_found) {
            memcpy(airpods_address, param->disc_res.bda, ESP_BD_ADDR_LEN);
            airpods_found = true;
            snprintf(airpods_name, sizeof(airpods_name), "%s; waiting to connect", name);
            ESP_LOGI(TAG, "found %s", name);
        }
        xSemaphoreGive(state_lock);
    } else if (event == ESP_BT_GAP_DISC_STATE_CHANGED_EVT &&
               param->disc_st_chg.state == ESP_BT_GAP_DISCOVERY_STOPPED) {
        xSemaphoreTake(state_lock, portMAX_DELAY);
        scanning = false;
        bool connect_airpods = airpods_found;
        xSemaphoreGive(state_lock);
        if (connect_airpods) {
            ESP_LOGI(TAG, "discovery complete; connecting AirPods profiles");
#if SAGE_BT_A2DP
            esp_a2d_source_connect(airpods_address);
#endif
            esp_hf_ag_slc_connect(airpods_address);
        } else {
            ESP_LOGI(TAG, "Bluetooth discovery complete; no AirPods found");
        }
    }
}

void bt_audio_start(void)
{
    state_lock = xSemaphoreCreateMutex();
    ESP_ERROR_CHECK(esp_bt_controller_mem_release(ESP_BT_MODE_BLE));
    esp_bt_controller_config_t controller_config = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_bt_controller_init(&controller_config));
    ESP_ERROR_CHECK(esp_bt_controller_enable(ESP_BT_MODE_CLASSIC_BT));
    ESP_ERROR_CHECK(esp_bluedroid_init());
    ESP_ERROR_CHECK(esp_bluedroid_enable());

    ESP_ERROR_CHECK(esp_bt_gap_register_callback(gap_callback));
    ESP_ERROR_CHECK(esp_bt_gap_set_device_name("Sage Wearable"));
    ESP_ERROR_CHECK(esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, ESP_BT_GENERAL_DISCOVERABLE));
    ESP_ERROR_CHECK(esp_avrc_tg_register_callback(avrc_target_callback));
    ESP_ERROR_CHECK(esp_avrc_tg_init());
    ESP_ERROR_CHECK(esp_a2d_register_callback(a2dp_callback));
#if SAGE_BT_A2DP
    ESP_ERROR_CHECK(esp_a2d_source_register_data_callback(a2dp_source_data));
    ESP_ERROR_CHECK(esp_a2d_source_init());
#endif
    ESP_ERROR_CHECK(esp_hf_ag_register_callback(hfp_callback));
    ESP_ERROR_CHECK(esp_hf_ag_init());
    /* Must come after esp_hf_ag_init(): receives the headset microphone PCM once SCO is open. */
    ESP_ERROR_CHECK(esp_hf_ag_register_data_callback(hfp_incoming_data, hfp_outgoing_data));
    ESP_LOGI(TAG, "Bluetooth enabled; use the web UI to scan for devices");
}

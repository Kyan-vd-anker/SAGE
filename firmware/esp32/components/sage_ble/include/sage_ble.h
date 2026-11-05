#pragma once

#include <stdint.h>

#include "esp_err.h"

esp_err_t sage_ble_start_scanner(void);
esp_err_t sage_ble_start_beacon(void);
int sage_ble_last_rssi(int64_t max_age_ms);

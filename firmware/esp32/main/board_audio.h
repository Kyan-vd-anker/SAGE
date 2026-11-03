#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

#define SAGE_AUDIO_RATE 16000
#define SAGE_MIC_TDM_CHANNELS 4

esp_err_t board_audio_init(void);
esp_err_t board_audio_read(int16_t *samples, size_t sample_count);
esp_err_t board_audio_write(const int16_t *samples, size_t sample_count);

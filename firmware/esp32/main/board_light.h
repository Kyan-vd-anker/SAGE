#pragma once

#include "esp_err.h"

typedef enum {
    BOARD_LIGHT_IDLE,
    BOARD_LIGHT_LISTENING,
    BOARD_LIGHT_THINKING,
    BOARD_LIGHT_SPEAKING,
    BOARD_LIGHT_ALERT,
    BOARD_LIGHT_MUTED,
    BOARD_LIGHT_ERROR,
    BOARD_LIGHT_SLEEPING,
} board_light_state_t;

esp_err_t board_light_init(void);
void board_light_set_state(board_light_state_t state);

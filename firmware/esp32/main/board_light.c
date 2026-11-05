#include "board_light.h"

#include <math.h>
#include <stddef.h>
#include <stdint.h>

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "led_strip.h"

#define LIGHT_GPIO GPIO_NUM_38
#define LIGHT_COUNT 7
#define LIGHT_FRAME_MS 100
#define LIGHT_MAX_BRIGHTNESS 0.24f

static const char *TAG = "board-light";
static led_strip_handle_t strip;
static portMUX_TYPE state_mux = portMUX_INITIALIZER_UNLOCKED;
static board_light_state_t ring_state = BOARD_LIGHT_IDLE;
static bool ring_light_off;

static void light_task(void *arg)
{
    const TickType_t frame_delay = pdMS_TO_TICKS(LIGHT_FRAME_MS);
    for (;;) {
        board_light_state_t state;
        bool light_off;
        portENTER_CRITICAL(&state_mux);
        state = ring_state;
        light_off = ring_light_off;
        portEXIT_CRITICAL(&state_mux);

        const int64_t now_us = esp_timer_get_time();
        const float seconds = (float)now_us / 1000000.0f;
        uint8_t red = 70;
        uint8_t green = 96;
        uint8_t blue = 150;
        switch (state) {
        case BOARD_LIGHT_LISTENING:
            red = 84;
            green = 146;
            blue = 255;
            break;
        case BOARD_LIGHT_THINKING:
        case BOARD_LIGHT_ALERT:
            red = 255;
            green = 188;
            blue = 76;
            break;
        case BOARD_LIGHT_SPEAKING:
            red = 74;
            green = 206;
            blue = 130;
            break;
        case BOARD_LIGHT_MUTED:
        case BOARD_LIGHT_ERROR:
            red = 244;
            green = 104;
            blue = 104;
            break;
        case BOARD_LIGHT_SLEEPING:
            red = 40;
            green = 52;
            blue = 80;
            break;
        case BOARD_LIGHT_IDLE:
        default:
            break;
        }

        for (size_t i = 0; i < LIGHT_COUNT; ++i) {
            float level = 1.0f;
            switch (state) {
            case BOARD_LIGHT_THINKING: {
                float phase = fmodf(seconds * 1.6f - (float)i / LIGHT_COUNT + 1.0f, 1.0f);
                level = 1.0f - phase;
                break;
            }
            case BOARD_LIGHT_SPEAKING:
                level = 0.55f + 0.45f * sinf(seconds * 6.0f + (float)i * 0.9f);
                break;
            case BOARD_LIGHT_LISTENING:
                level = 0.7f + 0.3f * sinf(seconds * 3.0f);
                break;
            case BOARD_LIGHT_ALERT:
                level = ((int)(seconds * 2.0f) % 2 == 0) ? 1.0f : 0.25f;
                break;
            case BOARD_LIGHT_IDLE:
                level = 0.55f;
                break;
            case BOARD_LIGHT_SLEEPING:
                level = 0.35f;
                break;
            case BOARD_LIGHT_MUTED:
            case BOARD_LIGHT_ERROR:
            default:
                break;
            }
            if (level < 0.12f) {
                level = 0.12f;
            } else if (level > 1.0f) {
                level = 1.0f;
            }
            float scale = light_off ? 0.0f : level * LIGHT_MAX_BRIGHTNESS;
            esp_err_t err = led_strip_set_pixel(
                strip, (uint32_t)i, (uint8_t)(red * scale), (uint8_t)(green * scale),
                (uint8_t)(blue * scale));
            if (err != ESP_OK) {
                ESP_LOGW(TAG, "Could not set LED %u: %s", (unsigned)i, esp_err_to_name(err));
                break;
            }
        }
        esp_err_t err = led_strip_refresh(strip);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "Could not refresh light ring: %s", esp_err_to_name(err));
        }
        vTaskDelay(frame_delay);
    }
}

esp_err_t board_light_init(void)
{
    const led_strip_config_t strip_config = {
        .strip_gpio_num = LIGHT_GPIO,
        .max_leds = LIGHT_COUNT,
        .led_model = LED_MODEL_WS2812,
        .color_component_format = LED_STRIP_COLOR_COMPONENT_FMT_GRB,
        .flags.invert_out = false,
    };
    const led_strip_rmt_config_t rmt_config = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = 10 * 1000 * 1000,
        .mem_block_symbols = 64,
        .flags.with_dma = false,
    };
    esp_err_t err = led_strip_new_rmt_device(&strip_config, &rmt_config, &strip);
    if (err != ESP_OK) {
        return err;
    }
    err = led_strip_clear(strip);
    if (err != ESP_OK) {
        led_strip_del(strip);
        strip = NULL;
        return err;
    }
    if (xTaskCreate(light_task, "sage-light", 3072, NULL, 2, NULL) != pdPASS) {
        led_strip_del(strip);
        strip = NULL;
        return ESP_FAIL;
    }
    return ESP_OK;
}

void board_light_set_state(board_light_state_t state)
{
    portENTER_CRITICAL(&state_mux);
    ring_state = state;
    portEXIT_CRITICAL(&state_mux);
}

void board_light_set_off(bool off)
{
    portENTER_CRITICAL(&state_mux);
    ring_light_off = off;
    portEXIT_CRITICAL(&state_mux);
}

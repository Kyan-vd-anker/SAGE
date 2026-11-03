#include "board_audio.h"

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "driver/i2s_tdm.h"
#include "esp_check.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "esp_io_expander_tca95xx_16bit.h"
#include "es7210_adc.h"
#include "es8311_codec.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define I2C_SDA GPIO_NUM_11
#define I2C_SCL GPIO_NUM_10
#define I2S_MCLK GPIO_NUM_12
#define I2S_BCLK GPIO_NUM_13
#define I2S_WS GPIO_NUM_14
#define I2S_DIN GPIO_NUM_15
#define I2S_DOUT GPIO_NUM_16

static i2c_master_bus_handle_t i2c_bus;
static i2s_chan_handle_t tx_channel;
static i2s_chan_handle_t rx_channel;
static esp_codec_dev_handle_t speaker;
static esp_codec_dev_handle_t microphone;

static esp_err_t init_i2s(void)
{
    i2s_chan_config_t channel_config = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    channel_config.dma_desc_num = 6;
    channel_config.dma_frame_num = 240;

    esp_err_t err = i2s_new_channel(&channel_config, &tx_channel, &rx_channel);
    if (err != ESP_OK) {
        return err;
    }

    const i2s_std_config_t speaker_config = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAGE_AUDIO_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(16, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_MCLK,
            .bclk = I2S_BCLK,
            .ws = I2S_WS,
            .dout = I2S_DOUT,
            .din = I2S_GPIO_UNUSED,
        },
    };
    i2s_tdm_config_t microphone_config = {
        .clk_cfg = {
            .sample_rate_hz = SAGE_AUDIO_RATE,
            .clk_src = I2S_CLK_SRC_DEFAULT,
            .mclk_multiple = I2S_MCLK_MULTIPLE_256,
            .bclk_div = 8,
        },
        .slot_cfg = {
            .data_bit_width = I2S_DATA_BIT_WIDTH_16BIT,
            .slot_bit_width = I2S_SLOT_BIT_WIDTH_AUTO,
            .slot_mode = I2S_SLOT_MODE_STEREO,
            .slot_mask = I2S_TDM_SLOT0 | I2S_TDM_SLOT1 | I2S_TDM_SLOT2 | I2S_TDM_SLOT3,
            .ws_width = I2S_TDM_AUTO_WS_WIDTH,
            .ws_pol = false,
            .bit_shift = true,
            .left_align = false,
            .big_endian = false,
            .bit_order_lsb = false,
            .skip_mask = false,
            .total_slot = I2S_TDM_AUTO_SLOT_NUM,
        },
        .gpio_cfg = {
            .mclk = I2S_MCLK,
            .bclk = I2S_BCLK,
            .ws = I2S_WS,
            .dout = I2S_GPIO_UNUSED,
            .din = I2S_DIN,
        },
    };
    microphone_config.slot_cfg.total_slot = 4;

    err = i2s_channel_init_std_mode(tx_channel, &speaker_config);
    if (err != ESP_OK) {
        return err;
    }
    err = i2s_channel_init_tdm_mode(rx_channel, &microphone_config);
    if (err != ESP_OK) {
        return err;
    }
    err = i2s_channel_enable(tx_channel);
    if (err != ESP_OK) {
        return err;
    }
    return i2s_channel_enable(rx_channel);
}

esp_err_t board_audio_init(void)
{
    const i2c_master_bus_config_t bus_config = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = I2C_SDA,
        .scl_io_num = I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    ESP_RETURN_ON_ERROR(i2c_new_master_bus(&bus_config, &i2c_bus), "sage-audio", "I2C setup");

    esp_io_expander_handle_t expander;
    ESP_RETURN_ON_ERROR(
        esp_io_expander_new_i2c_tca95xx_16bit(
            i2c_bus, ESP_IO_EXPANDER_I2C_TCA9555_ADDRESS_000, &expander),
        "sage-audio", "TCA9555 not found");
    ESP_RETURN_ON_ERROR(
        esp_io_expander_set_dir(expander, IO_EXPANDER_PIN_NUM_8, IO_EXPANDER_OUTPUT),
        "sage-audio", "amplifier pin setup");
    ESP_RETURN_ON_ERROR(
        esp_io_expander_set_level(expander, IO_EXPANDER_PIN_NUM_8, 1),
        "sage-audio", "enable speaker amplifier");
    vTaskDelay(pdMS_TO_TICKS(50));

    ESP_RETURN_ON_ERROR(init_i2s(), "sage-audio", "I2S setup");

    const audio_codec_i2s_cfg_t i2s_config = {
        .port = I2S_NUM_0,
        .rx_handle = rx_channel,
        .tx_handle = tx_channel,
    };
    const audio_codec_data_if_t *data_interface = audio_codec_new_i2s_data(&i2s_config);
    if (!data_interface) {
        return ESP_FAIL;
    }

    audio_codec_i2c_cfg_t control_config = {
        .port = I2C_NUM_0,
        .addr = ES8311_CODEC_DEFAULT_ADDR,
        .bus_handle = i2c_bus,
    };
    const audio_codec_ctrl_if_t *speaker_control = audio_codec_new_i2c_ctrl(&control_config);
    const audio_codec_gpio_if_t *gpio_interface = audio_codec_new_gpio();
    es8311_codec_cfg_t speaker_codec_config = {
        .ctrl_if = speaker_control,
        .gpio_if = gpio_interface,
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_DAC,
        .pa_pin = -1,
        .use_mclk = true,
        .hw_gain = {.pa_voltage = 5.0, .codec_dac_voltage = 3.3},
    };
    const esp_codec_dev_cfg_t speaker_device_config = {
        .dev_type = ESP_CODEC_DEV_TYPE_OUT,
        .codec_if = es8311_codec_new(&speaker_codec_config),
        .data_if = data_interface,
    };
    speaker = esp_codec_dev_new(&speaker_device_config);

    control_config.addr = ES7210_CODEC_DEFAULT_ADDR;
    const audio_codec_ctrl_if_t *microphone_control = audio_codec_new_i2c_ctrl(&control_config);
    es7210_codec_cfg_t microphone_codec_config = {
        .ctrl_if = microphone_control,
        .mic_selected = ES7210_SEL_MIC1 | ES7210_SEL_MIC2 | ES7210_SEL_MIC3 | ES7210_SEL_MIC4,
    };
    const esp_codec_dev_cfg_t microphone_device_config = {
        .dev_type = ESP_CODEC_DEV_TYPE_IN,
        .codec_if = es7210_codec_new(&microphone_codec_config),
        .data_if = data_interface,
    };
    microphone = esp_codec_dev_new(&microphone_device_config);
    if (!speaker || !microphone) {
        return ESP_FAIL;
    }

    const esp_codec_dev_sample_info_t input_format = {
        .bits_per_sample = 16,
        .channel = SAGE_MIC_TDM_CHANNELS,
        .channel_mask = ESP_CODEC_DEV_MAKE_CHANNEL_MASK(0) |
                        ESP_CODEC_DEV_MAKE_CHANNEL_MASK(1) |
                        ESP_CODEC_DEV_MAKE_CHANNEL_MASK(2) |
                        ESP_CODEC_DEV_MAKE_CHANNEL_MASK(3),
        .sample_rate = SAGE_AUDIO_RATE,
    };
    if (esp_codec_dev_open(microphone, &input_format) != ESP_CODEC_DEV_OK) {
        return ESP_FAIL;
    }

    const esp_codec_dev_sample_info_t output_format = {
        .bits_per_sample = 16,
        .channel = 1,
        .sample_rate = SAGE_AUDIO_RATE,
    };
    if (esp_codec_dev_open(speaker, &output_format) != ESP_CODEC_DEV_OK) {
        return ESP_FAIL;
    }
    return esp_codec_dev_set_out_vol(speaker, 65) == ESP_CODEC_DEV_OK ? ESP_OK : ESP_FAIL;
}

esp_err_t board_audio_read(int16_t *samples, size_t sample_count)
{
    return esp_codec_dev_read(microphone, samples, sample_count * sizeof(int16_t)) == ESP_CODEC_DEV_OK
               ? ESP_OK
               : ESP_FAIL;
}

esp_err_t board_audio_write(const int16_t *samples, size_t sample_count)
{
    return esp_codec_dev_write(speaker, (void *)samples, sample_count * sizeof(int16_t)) ==
                   ESP_CODEC_DEV_OK
               ? ESP_OK
               : ESP_FAIL;
}

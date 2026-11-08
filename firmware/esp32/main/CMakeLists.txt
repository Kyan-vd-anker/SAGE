set(SAGE_REQUIRES
    esp_codec_dev esp_driver_gpio esp_driver_i2c esp_driver_i2s esp_http_server
    esp_hw_support lwip esp_event esp_http_client esp_netif esp-sr esp_timer esp_wifi
    esp_io_expander esp_io_expander_tca95xx_16bit led_strip json mbedtls nvs_flash
)
idf_component_register(
    SRCS "sage_speaker.c" "board_audio.c" "board_light.c"
    INCLUDE_DIRS "."
    REQUIRES ${SAGE_REQUIRES}
    PRIV_REQUIRES sage_ble
)

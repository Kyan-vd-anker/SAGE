idf_component_register(
    SRCS "main.c" "bt_audio.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES bt driver esp_http_server esp_netif esp_wifi nvs_flash
)

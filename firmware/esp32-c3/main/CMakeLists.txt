idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    REQUIRES bt esp_event esp_http_client esp_netif esp_timer esp_wifi lwip mbedtls nvs_flash sage_ble
)

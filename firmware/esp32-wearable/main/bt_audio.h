#pragma once

#include <stdbool.h>
#include <stddef.h>

void bt_audio_start(void);
bool bt_audio_is_push_to_talk(void);
void bt_audio_set_push_to_talk(bool active);
void bt_audio_scan(void);
void bt_audio_get_status(char *status, size_t status_size);
void bt_audio_get_devices_json(char *json, size_t json_size);
bool bt_audio_connect_device(const char *address);

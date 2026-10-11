#pragma once
#include <3ds.h>

// Controls reference on the top screen, touch UI on the bottom screen.
void ui_init(const char *host, bool audio_ok);
void ui_exit(void);

// Shows a message at the bottom of the top screen for a few seconds.
void ui_error(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

// Handles touch input and draws one frame; waits for vblank.
void ui_frame(u32 down, u32 held, u32 up);

#pragma once
#include <stdbool.h>

typedef enum { AUDIO_OFF, AUDIO_CONNECTING, AUDIO_BUFFERING, AUDIO_PLAYING, AUDIO_FAILED } AudioState;

// Starts the DSP. False if it can't (usually a missing sdmc:/3ds/dspfirm.cdc).
bool audio_init(void);
void audio_exit(void);

// Streams the Pi's audio into the headphone jack until audio_stop() or the stream fails.
void audio_start(const char *host);
void audio_stop(void);

// How late the 3DS sound is, in ms. Sent to the Pi, which shifts Kodi's audio earlier by that much.
void audio_set_delay(int ms);

AudioState audio_state(void);
int audio_buffered_ms(void);
const char *audio_error(void);

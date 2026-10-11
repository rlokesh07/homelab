#include "audio.h"

#include <3ds.h>
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#define PORT 8091
#define RATE 32000
#define FRAME_BYTES 4  // 16-bit stereo
#define BUF_FRAMES 512
#define NUM_BUFS 8
#define RING_BYTES (RATE * FRAME_BYTES)  // 1 s
#define MS_TO_BYTES(ms) ((ms) * RATE / 1000 * FRAME_BYTES)
#define START_BYTES MS_TO_BYTES(120)  // buffered before playback starts or resumes
#define MAX_BYTES MS_TO_BYTES(400)    // beyond this, skip ahead to START_BYTES to stay in sync
#define CHANNEL 0

static bool dsp_ready;
static Thread thread;
static volatile bool running;
static volatile AudioState state = AUDIO_OFF;
static volatile int delay_ms;
static volatile int buffered_ms;
static char error_text[96];
static char host[64];

static ndspWaveBuf wavebufs[NUM_BUFS];
static s16 *wave_data;
static u8 *ring;
static size_t ring_start, ring_len;

bool audio_init(void) {
    if (R_FAILED(ndspInit())) return false;
    wave_data = linearAlloc(NUM_BUFS * BUF_FRAMES * FRAME_BYTES);
    ring = malloc(RING_BYTES);
    if (!wave_data || !ring) {
        ndspExit();
        return false;
    }
    dsp_ready = true;
    return true;
}

void audio_exit(void) {
    audio_stop();
    if (!dsp_ready) return;
    ndspExit();
    linearFree(wave_data);
    free(ring);
    dsp_ready = false;
}

void audio_set_delay(int ms) { delay_ms = ms; }
AudioState audio_state(void) { return state; }
int audio_buffered_ms(void) { return buffered_ms; }
const char *audio_error(void) { return error_text; }

static void ring_drop(size_t n) {
    ring_start = (ring_start + n) % RING_BYTES;
    ring_len -= n;
}

static void ring_write(const u8 *data, size_t n) {
    if (ring_len + n > MAX_BYTES) {
        size_t excess = ring_len + n - START_BYTES;
        size_t drop = excess > ring_len ? ring_len : excess;
        ring_drop(drop - drop % FRAME_BYTES);
    }
    size_t end = (ring_start + ring_len) % RING_BYTES;
    size_t first = RING_BYTES - end < n ? RING_BYTES - end : n;
    memcpy(ring + end, data, first);
    memcpy(ring, data + first, n - first);
    ring_len += n;
}

static void ring_read(u8 *out, size_t n) {
    size_t first = RING_BYTES - ring_start < n ? RING_BYTES - ring_start : n;
    memcpy(out, ring + ring_start, first);
    memcpy(out + first, ring, n - first);
    ring_drop(n);
}

static void feed_dsp(void) {
    const size_t buf_bytes = BUF_FRAMES * FRAME_BYTES;
    int queued = 0;
    for (int i = 0; i < NUM_BUFS; i++) {
        ndspWaveBuf *wb = &wavebufs[i];
        if (wb->status == NDSP_WBUF_QUEUED || wb->status == NDSP_WBUF_PLAYING) {
            queued++;
            continue;
        }
        if (state == AUDIO_BUFFERING && ring_len < START_BYTES) continue;
        if (ring_len < buf_bytes) continue;
        state = AUDIO_PLAYING;
        ring_read((u8 *)wb->data_pcm16, buf_bytes);
        wb->nsamples = BUF_FRAMES;
        DSP_FlushDataCache(wb->data_pcm16, buf_bytes);
        ndspChnWaveBufAdd(CHANNEL, wb);
        queued++;
    }
    if (queued == 0 && state == AUDIO_PLAYING) state = AUDIO_BUFFERING;
    buffered_ms = (int)((ring_len / FRAME_BYTES + queued * BUF_FRAMES) * 1000 / RATE);
}

static void fail(const char *what) {
    snprintf(error_text, sizeof error_text, "%s: %s (%d)", what, strerror(errno), errno);
    state = AUDIO_FAILED;
}

static void stream(void *arg) {
    (void)arg;
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons(PORT)};
    inet_aton(host, &addr.sin_addr);
    int sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) {
        fail("Audio socket failed");
        return;
    }
    if (connect(sock, (struct sockaddr *)&addr, sizeof addr) < 0) {
        fail("Can't reach the Pi's audio stream");
        close(sock);
        return;
    }

    state = AUDIO_BUFFERING;
    int sent_delay = -1;
    u64 last_sent = 0;
    u8 chunk[4096];
    while (running) {
        // The Pi treats a quiet 3DS as gone, so the delay doubles as a keepalive.
        u64 now = osGetTime();
        if (delay_ms != sent_delay || now - last_sent >= 1000) {
            char line[32];
            int len = snprintf(line, sizeof line, "delay %d\n", delay_ms);
            if (send(sock, line, len, 0) != len) {
                fail("Audio stream lost");
                break;
            }
            sent_delay = delay_ms;
            last_sent = now;
        }

        struct pollfd p = {.fd = sock, .events = POLLIN};
        if (poll(&p, 1, 5) > 0) {
            ssize_t n = recv(sock, chunk, sizeof chunk, 0);
            if (n <= 0) {
                if (n == 0) errno = ECONNRESET;
                fail("Audio stream ended");
                break;
            }
            ring_write(chunk, n);
        }
        feed_dsp();
    }
    close(sock);
    ndspChnWaveBufClear(CHANNEL);
}

void audio_start(const char *target) {
    if (!dsp_ready) return;
    audio_stop();
    snprintf(host, sizeof host, "%s", target);
    error_text[0] = '\0';
    ring_start = ring_len = 0;
    buffered_ms = 0;

    ndspSetOutputMode(NDSP_OUTPUT_STEREO);
    ndspChnReset(CHANNEL);
    ndspChnSetInterp(CHANNEL, NDSP_INTERP_LINEAR);
    ndspChnSetRate(CHANNEL, RATE);
    ndspChnSetFormat(CHANNEL, NDSP_FORMAT_STEREO_PCM16);
    float mix[12] = {1.0f, 1.0f};
    ndspChnSetMix(CHANNEL, mix);
    memset(wavebufs, 0, sizeof wavebufs);
    for (int i = 0; i < NUM_BUFS; i++) wavebufs[i].data_pcm16 = wave_data + i * BUF_FRAMES * 2;

    s32 priority = 0x30;
    svcGetThreadPriority(&priority, CUR_THREAD_HANDLE);
    state = AUDIO_CONNECTING;
    running = true;
    // Higher priority than the UI so its blocking HTTP calls don't starve playback.
    thread = threadCreate(stream, NULL, 32 * 1024, priority - 1, -2, false);
    if (!thread) {
        running = false;
        snprintf(error_text, sizeof error_text, "Couldn't start the audio thread");
        state = AUDIO_FAILED;
    }
}

void audio_stop(void) {
    if (thread) {
        running = false;
        threadJoin(thread, U64_MAX);
        threadFree(thread);
        thread = NULL;
    }
    state = AUDIO_OFF;
    buffered_ms = 0;
}

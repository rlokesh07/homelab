#include "ui.h"

#include <citro2d.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "audio.h"
#include "kodi.h"

#define SCREEN_W 320.0f
#define SCREEN_H 240.0f
#define TAB_H 32.0f
#define ROW_H 30.0f
#define LIST_H (SCREEN_H - TAB_H)
#define PAD 10.0f
#define TEXT_SCALE 0.5f
#define TEXT_H 15.0f  // system font line height at TEXT_SCALE
#define DRAG_THRESHOLD 6
#define POLL_FRAMES 60

#define BAR_X 20.0f
#define BAR_Y 128.0f
#define BAR_W 280.0f
#define BAR_H 10.0f
#define BUTTON_Y 168.0f
#define BUTTON_H 50.0f
#define BUTTON_W 135.0f
#define PLAY_X 20.0f
#define STOP_X 165.0f

#define SEGMENT_Y 60.0f
#define SEGMENT_H 40.0f
#define STEP_Y 164.0f
#define STEP_W 64.0f
#define STEP_H 40.0f
#define MINUS_X 20.0f
#define PLUS_X (SCREEN_W - 20.0f - STEP_W)
#define DELAY_STEP 25
#define DELAY_MAX 2000
#define AUDIO_RETRY_FRAMES (3 * 60)
#define STATE_PATH "sdmc:/3ds/kodi-remote-state.cfg"

#define TOP_W 400.0f
#define HEADER_H 40.0f
#define HINT_Y 58.0f
#define HINT_STEP 26.0f
#define HINT_ROWS 6
#define PILL_W 78.0f
#define PILL_H 20.0f
#define FOOTER_Y 212.0f
#define ERROR_FRAMES (5 * 60)

#define COLOR_BG C2D_Color32(0x12, 0x14, 0x1a, 0xff)
#define COLOR_HEADER_DARK C2D_Color32(0x1c, 0x3a, 0x58, 0xff)
#define COLOR_ERROR_BG C2D_Color32(0x5a, 0x1e, 0x1e, 0xff)
#define COLOR_ERROR C2D_Color32(0xff, 0xb4, 0xa8, 0xff)
#define COLOR_ROW_ALT C2D_Color32(0x1a, 0x1d, 0x25, 0xff)
#define COLOR_TAB C2D_Color32(0x22, 0x26, 0x30, 0xff)
#define COLOR_ACTIVE C2D_Color32(0x2e, 0x5c, 0x8a, 0xff)
#define COLOR_TEXT C2D_Color32(0xee, 0xee, 0xee, 0xff)
#define COLOR_DIM C2D_Color32(0x90, 0x96, 0xa0, 0xff)
#define COLOR_TRACK C2D_Color32(0x33, 0x38, 0x44, 0xff)
#define COLOR_ACCENT C2D_Color32(0x4a, 0x9e, 0xe8, 0xff)

typedef enum { TAB_MOVIES, TAB_SHOWS, TAB_PLAYING, TAB_AUDIO, TAB_COUNT } Tab;
static const char *TAB_NAMES[TAB_COUNT] = {"Movies", "TV Shows", "Playing", "Audio"};

typedef struct {
    u32 keys;  // lights up the hint while held
    const char *button;
    const char *action;
} Hint;

// First HINT_ROWS go in the left column, the rest in the right.
static const Hint HINTS[] = {
    {KEY_DUP | KEY_DDOWN | KEY_DLEFT | KEY_DRIGHT, "D-Pad", "Navigate"},
    {KEY_CPAD_UP | KEY_CPAD_DOWN | KEY_CPAD_LEFT | KEY_CPAD_RIGHT, "Circle Pad", "Navigate"},
    {KEY_A, "A", "Select"},
    {KEY_B, "B", "Back"},
    {KEY_X, "X", "Menu"},
    {KEY_Y, "Y", "Info"},
    {KEY_START, "START", "Play / Pause"},
    {KEY_SELECT, "SELECT", "Home"},
    {KEY_L | KEY_ZL, "L", "Volume down"},
    {KEY_R | KEY_ZR, "R", "Volume up"},
    {KEY_TOUCH, "Touch", "Browse & seek"},
    {0, "HOME", "Quit"},
};
#define HINT_COUNT (int)(sizeof HINTS / sizeof HINTS[0])

typedef struct {
    KodiList list;
    C2D_TextBuf textbuf;
    C2D_Text *texts;
    char back_label[160];  // non-empty when this page is inside a show
    C2D_Text back_text;
    bool loaded;
    char message[160];  // shown under the rows when the list is empty or failed to load
    float scroll;
    int tvshowid, season;
} ListPage;

typedef enum {
    TOUCH_NONE,
    TOUCH_TABS,
    TOUCH_LIST,
    TOUCH_SEEK,
    TOUCH_PLAY,
    TOUCH_STOP,
    TOUCH_TV,
    TOUCH_3DS,
    TOUCH_MINUS,
    TOUCH_PLUS,
} TouchTarget;

static C3D_RenderTarget *top_screen, *screen;
static C2D_TextBuf static_buf, frame_buf, scratch_buf;
static C2D_Text tab_texts[TAB_COUNT];
static C2D_Text title_text, host_text;
static C2D_Text hint_buttons[HINT_COUNT], hint_actions[HINT_COUNT];

static char error_message[128];
static int error_frames;

static char kodi_host[64];
static bool audio_available, audio_wanted;
static int audio_delay = 250;
static int audio_retry_timer;
static int step_hold_frames;

static Tab tab = TAB_MOVIES;
static ListPage movies = {.list.kind = KODI_MOVIES};
static ListPage shows[3] = {{.list.kind = KODI_SHOWS}, {.list.kind = KODI_SEASONS}, {.list.kind = KODI_EPISODES}};
static int show_depth;
static ListPage *pending_load;

static KodiNowPlaying now;
static bool now_ok = true;
static char now_error[128];
static int poll_timer;
static int frames_since_poll;
static float seek_preview = -1;

static TouchTarget touch_target;
static touchPosition touch_start, touch_last;
static bool dragging;
static float scroll_start;

void ui_error(const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    vsnprintf(error_message, sizeof error_message, fmt, args);
    va_end(args);
    error_frames = ERROR_FRAMES;
}

static ListPage *current_page(void) {
    if (tab == TAB_MOVIES) return &movies;
    if (tab == TAB_SHOWS) return &shows[show_depth];
    return NULL;
}

static void load_delay(void) {
    FILE *f = fopen(STATE_PATH, "r");
    if (!f) return;
    int value;
    if (fscanf(f, "delay=%d", &value) == 1 && value >= 0 && value <= DELAY_MAX) audio_delay = value;
    fclose(f);
}

static void save_delay(void) {
    FILE *f = fopen(STATE_PATH, "w");
    if (!f) return;
    fprintf(f, "delay=%d\n", audio_delay);
    fclose(f);
}

static void step_delay(int direction) {
    int value = audio_delay + direction * DELAY_STEP;
    audio_delay = value < 0 ? 0 : value > DELAY_MAX ? DELAY_MAX : value;
    audio_set_delay(audio_delay);
}

static void set_audio_on_3ds(bool on) {
    if (on == audio_wanted) return;
    audio_wanted = on;
    audio_retry_timer = 0;
    if (on) {
        audio_set_delay(audio_delay);
        audio_start(kodi_host);
    } else {
        audio_stop();
    }
}

static float text_width(const C2D_Text *text, float scale) {
    float width;
    C2D_TextGetDimensions(text, scale, scale, &width, NULL);
    return width;
}

// Parses str into buf, shortened with "..." if it's wider than max_width.
static void parse_fitted(C2D_Text *text, C2D_TextBuf buf, const char *str, float scale, float max_width) {
    char fitted[192];
    snprintf(fitted, sizeof fitted, "%s", str);
    size_t keep = strlen(str);
    for (;;) {
        C2D_TextBufClear(scratch_buf);
        C2D_Text probe;
        C2D_TextParse(&probe, scratch_buf, fitted);
        float width = text_width(&probe, scale);
        if (width <= max_width || keep == 0) break;
        size_t estimate = (size_t)(keep * (max_width / width));
        keep = estimate < keep ? estimate : keep - 1;
        keep = keep > 3 ? keep - 3 : 0;
        snprintf(fitted, sizeof fitted, "%.*s...", (int)utf8_fit(str, keep), str);
    }
    C2D_TextParse(text, buf, fitted);
    C2D_TextOptimize(text);
}

static int row_count(const ListPage *p) { return (p->back_label[0] ? 1 : 0) + p->list.count; }

static float max_scroll(const ListPage *p) {
    float content = row_count(p) * ROW_H;
    return content > LIST_H ? content - LIST_H : 0;
}

static void reset_page(ListPage *p) {
    if (p->textbuf) C2D_TextBufDelete(p->textbuf);
    free(p->texts);
    kodi_list_free(&p->list);
    p->textbuf = NULL;
    p->texts = NULL;
    p->loaded = false;
    p->message[0] = '\0';
    p->scroll = 0;
}

static void load_page(ListPage *p) {
    reset_page(p);
    bool ok = false;
    switch (p->list.kind) {
        case KODI_MOVIES: ok = kodi_movies(&p->list); break;
        case KODI_SHOWS: ok = kodi_shows(&p->list); break;
        case KODI_SEASONS: ok = kodi_seasons(p->tvshowid, &p->list); break;
        case KODI_EPISODES: ok = kodi_episodes(p->tvshowid, p->season, &p->list); break;
    }
    p->loaded = true;

    if (!ok) {
        snprintf(p->message, sizeof p->message, "%s\n\nTap to retry.", kodi_error());
    } else if (p->list.count == 0) {
        bool top_level = p->list.kind == KODI_MOVIES || p->list.kind == KODI_SHOWS;
        snprintf(p->message, sizeof p->message, "%s",
                 top_level ? "Nothing in the library.\nIs the media drive plugged in?\n\nTap to retry." : "Nothing here.");
    }

    size_t glyphs = 16 + strlen(p->back_label);
    for (int i = 0; i < p->list.count; i++) glyphs += strlen(p->list.items[i].label) + 3;
    p->textbuf = C2D_TextBufNew(glyphs);
    p->texts = p->list.count ? calloc(p->list.count, sizeof(C2D_Text)) : NULL;
    for (int i = 0; i < p->list.count && p->texts; i++) {
        parse_fitted(&p->texts[i], p->textbuf, p->list.items[i].label, TEXT_SCALE, SCREEN_W - 2 * PAD);
    }
    if (p->back_label[0]) {
        parse_fitted(&p->back_text, p->textbuf, p->back_label, TEXT_SCALE, SCREEN_W - 2 * PAD);
    }
}

static void open_page(int depth, KodiListKind kind, int tvshowid, int season, const char *title) {
    ListPage *p = &shows[depth];
    reset_page(p);
    p->list.kind = kind;
    p->tvshowid = tvshowid;
    p->season = season;
    snprintf(p->back_label, sizeof p->back_label, "< %s", title);
    show_depth = depth;
}

static void switch_tab(Tab next) {
    tab = next;
    if (tab == TAB_PLAYING) poll_timer = 0;
}

static void play(KodiListKind kind, const KodiItem *item) {
    bool ok = kind == KODI_MOVIES ? kodi_play_movie(item->id) : kodi_play_episode(item->id);
    if (!ok) {
        ui_error("Play failed: %s", kodi_error());
        return;
    }
    switch_tab(TAB_PLAYING);
    poll_timer = 30;  // give Kodi a moment to start the player
}

static void activate_row(ListPage *p, int row) {
    if (p->back_label[0]) {
        if (row == 0) {
            show_depth--;
            return;
        }
        row--;
    }
    if (row < 0 || row >= p->list.count) {
        if (p->list.count == 0 && p->message[0]) pending_load = p;
        return;
    }

    const KodiItem *item = &p->list.items[row];
    switch (p->list.kind) {
        case KODI_MOVIES:
        case KODI_EPISODES: play(p->list.kind, item); break;
        case KODI_SHOWS: open_page(1, KODI_SEASONS, item->id, 0, item->label); break;
        case KODI_SEASONS: open_page(2, KODI_EPISODES, p->tvshowid, item->id, item->label); break;
    }
}

static bool in_rect(touchPosition t, float x, float y, float w, float h) {
    return t.px >= x && t.px < x + w && t.py >= y && t.py < y + h;
}

static float clampf(float v, float lo, float hi) { return v < lo ? lo : v > hi ? hi : v; }

static TouchTarget classify(touchPosition t) {
    if (t.py < TAB_H) return TOUCH_TABS;
    if (tab == TAB_AUDIO) {
        if (!audio_available) return TOUCH_NONE;
        if (in_rect(t, PLAY_X, SEGMENT_Y, BUTTON_W, SEGMENT_H)) return TOUCH_TV;
        if (in_rect(t, STOP_X, SEGMENT_Y, BUTTON_W, SEGMENT_H)) return TOUCH_3DS;
        if (in_rect(t, MINUS_X, STEP_Y, STEP_W, STEP_H)) return TOUCH_MINUS;
        if (in_rect(t, PLUS_X, STEP_Y, STEP_W, STEP_H)) return TOUCH_PLUS;
        return TOUCH_NONE;
    }
    if (tab != TAB_PLAYING) return TOUCH_LIST;
    if (!now.active) return TOUCH_NONE;
    if (in_rect(t, BAR_X - 10, BAR_Y - 20, BAR_W + 20, BAR_H + 40)) return TOUCH_SEEK;
    if (in_rect(t, PLAY_X, BUTTON_Y, BUTTON_W, BUTTON_H)) return TOUCH_PLAY;
    if (in_rect(t, STOP_X, BUTTON_Y, BUTTON_W, BUTTON_H)) return TOUCH_STOP;
    return TOUCH_NONE;
}

static void tap_tab(touchPosition t) {
    Tab tapped = (Tab)(t.px * TAB_COUNT / (int)SCREEN_W);
    if (tapped != tab) {
        switch_tab(tapped);
    } else if (tab == TAB_SHOWS && show_depth > 0) {
        show_depth = 0;
    } else if (tab == TAB_PLAYING) {
        poll_timer = 0;
    } else {
        pending_load = current_page();  // tapping the open tab refreshes it
    }
}

static void handle_touch(u32 down, u32 held, u32 up) {
    touchPosition pos;
    hidTouchRead(&pos);
    ListPage *page = current_page();

    if (down & KEY_TOUCH) {
        touch_start = touch_last = pos;
        dragging = false;
        scroll_start = page ? page->scroll : 0;
        touch_target = classify(pos);
    } else if (held & KEY_TOUCH) {
        touch_last = pos;
    }

    if (touch_target == TOUCH_LIST && page && (held & KEY_TOUCH)) {
        int dy = touch_last.py - touch_start.py;
        if (abs(dy) > DRAG_THRESHOLD) dragging = true;
        if (dragging) page->scroll = clampf(scroll_start - dy, 0, max_scroll(page));
    }
    if (touch_target == TOUCH_SEEK && (held & KEY_TOUCH)) {
        seek_preview = clampf((touch_last.px - BAR_X) / BAR_W, 0, 1) * 100;
    }
    if ((touch_target == TOUCH_MINUS || touch_target == TOUCH_PLUS) && (held & KEY_TOUCH)) {
        // Step on press, then repeat while held.
        int frames = step_hold_frames++;
        if (frames == 0 || (frames >= 20 && frames % 5 == 0)) step_delay(touch_target == TOUCH_PLUS ? 1 : -1);
    }

    if (!(up & KEY_TOUCH)) return;
    switch (touch_target) {
        case TOUCH_TABS:
            if (touch_last.py < TAB_H) tap_tab(touch_start);
            break;
        case TOUCH_LIST:
            if (!dragging && page && page->loaded) {
                activate_row(page, (int)((touch_start.py - TAB_H + page->scroll) / ROW_H));
            }
            break;
        case TOUCH_SEEK:
            if (seek_preview >= 0 && kodi_seek(now.playerid, seek_preview)) {
                now.percentage = seek_preview;
                now.time = (int)(now.total * seek_preview / 100);
                frames_since_poll = 0;
                poll_timer = 30;
            } else if (seek_preview >= 0) {
                ui_error("Seek failed: %s", kodi_error());
            }
            seek_preview = -1;
            break;
        case TOUCH_PLAY:
            if (!kodi_play_pause(now.playerid)) ui_error("Play/Pause failed: %s", kodi_error());
            poll_timer = 10;
            break;
        case TOUCH_STOP:
            if (!kodi_stop(now.playerid)) ui_error("Stop failed: %s", kodi_error());
            poll_timer = 10;
            break;
        case TOUCH_TV:
        case TOUCH_3DS: set_audio_on_3ds(touch_target == TOUCH_3DS); break;
        case TOUCH_MINUS:
        case TOUCH_PLUS:
            step_hold_frames = 0;
            save_delay();
            break;
        case TOUCH_NONE: break;
    }
    touch_target = TOUCH_NONE;
}

static void poll_now_playing(void) {
    frames_since_poll++;
    if (tab != TAB_PLAYING || touch_target == TOUCH_SEEK || --poll_timer > 0) return;
    now_ok = kodi_now_playing(&now);
    frames_since_poll = 0;
    if (!now_ok) snprintf(now_error, sizeof now_error, "%s", kodi_error());
    poll_timer = now_ok ? POLL_FRAMES : 5 * POLL_FRAMES;
}

static void draw_text(const char *str, float x, float y, float scale, u32 color, u32 flags) {
    C2D_Text text;
    C2D_TextParse(&text, frame_buf, str);
    C2D_DrawText(&text, C2D_WithColor | flags, x, y, 0, scale, scale, color, SCREEN_W - 2 * PAD);
}

static void draw_message(const char *message, float y) {
    draw_text(message, SCREEN_W / 2, y, TEXT_SCALE, COLOR_DIM, C2D_AlignCenter | C2D_WordWrap);
}

static void draw_list(ListPage *p) {
    if (!p->loaded) {
        draw_message("Loading...", TAB_H + 40);
        pending_load = p;
        return;
    }

    int rows = row_count(p);
    int first = (int)(p->scroll / ROW_H);
    int pressed = (touch_target == TOUCH_LIST && !dragging) ? (int)((touch_start.py - TAB_H + p->scroll) / ROW_H) : -1;
    for (int row = first; row < rows && row <= first + (int)(LIST_H / ROW_H) + 1; row++) {
        float y = TAB_H + row * ROW_H - p->scroll;
        if (row == pressed) {
            C2D_DrawRectSolid(0, y, 0, SCREEN_W, ROW_H, COLOR_ACTIVE);
        } else if (row % 2) {
            C2D_DrawRectSolid(0, y, 0, SCREEN_W, ROW_H, COLOR_ROW_ALT);
        }
        bool back = p->back_label[0] && row == 0;
        const C2D_Text *text = back ? &p->back_text : &p->texts[row - (p->back_label[0] ? 1 : 0)];
        C2D_DrawText(text, C2D_WithColor, PAD, y + (ROW_H - TEXT_H) / 2, 0, TEXT_SCALE, TEXT_SCALE,
                     back ? COLOR_ACCENT : COLOR_TEXT);
    }

    if (p->message[0]) draw_message(p->message, TAB_H + rows * ROW_H + 30);

    float limit = max_scroll(p);
    if (limit > 0) {
        float content = rows * ROW_H;
        float h = LIST_H * LIST_H / content;
        C2D_DrawRectSolid(SCREEN_W - 4, TAB_H + (LIST_H - h) * p->scroll / limit, 0, 3, h, COLOR_DIM);
    }
}

static void format_time(char *out, size_t size, int s) {
    if (s >= 3600) {
        snprintf(out, size, "%d:%02d:%02d", s / 3600, s / 60 % 60, s % 60);
    } else {
        snprintf(out, size, "%d:%02d", s / 60, s % 60);
    }
}

static void draw_playing(void) {
    if (!now_ok) {
        draw_message(now_error, TAB_H + 60);
        return;
    }
    if (!now.active) {
        draw_message("Nothing playing", TAB_H + 60);
        return;
    }

    C2D_Text title;
    parse_fitted(&title, frame_buf, now.title, 0.6f, SCREEN_W - 2 * PAD);
    C2D_DrawText(&title, C2D_WithColor | C2D_AlignCenter, SCREEN_W / 2, 52, 0, 0.6f, 0.6f, COLOR_TEXT);
    if (now.paused) draw_text("Paused", SCREEN_W / 2, 76, TEXT_SCALE, COLOR_DIM, C2D_AlignCenter);

    // Advance the clock between polls so it doesn't tick once a second in jumps.
    float elapsed = now.paused ? 0 : frames_since_poll / 60.0f;
    float time = seek_preview >= 0 ? now.total * seek_preview / 100 : clampf(now.time + elapsed, 0, now.total);
    float fraction = now.total > 0 ? time / now.total : now.percentage / 100;

    char position[16], total[16], line[40];
    format_time(position, sizeof position, (int)time);
    format_time(total, sizeof total, now.total);
    snprintf(line, sizeof line, "%s / %s", position, total);
    draw_text(line, SCREEN_W / 2, 100, TEXT_SCALE, COLOR_TEXT, C2D_AlignCenter);

    C2D_DrawRectSolid(BAR_X, BAR_Y, 0, BAR_W, BAR_H, COLOR_TRACK);
    C2D_DrawRectSolid(BAR_X, BAR_Y, 0, BAR_W * fraction, BAR_H, COLOR_ACCENT);
    C2D_DrawRectSolid(BAR_X + BAR_W * fraction - 3, BAR_Y - 5, 0, 6, BAR_H + 10, COLOR_TEXT);

    C2D_DrawRectSolid(PLAY_X, BUTTON_Y, 0, BUTTON_W, BUTTON_H, touch_target == TOUCH_PLAY ? COLOR_ACTIVE : COLOR_TAB);
    C2D_DrawRectSolid(STOP_X, BUTTON_Y, 0, BUTTON_W, BUTTON_H, touch_target == TOUCH_STOP ? COLOR_ACTIVE : COLOR_TAB);
    float label_y = BUTTON_Y + (BUTTON_H - TEXT_H * 1.2f) / 2;
    draw_text(now.paused ? "Play" : "Pause", PLAY_X + BUTTON_W / 2, label_y, 0.6f, COLOR_TEXT, C2D_AlignCenter);
    draw_text("Stop", STOP_X + BUTTON_W / 2, label_y, 0.6f, COLOR_TEXT, C2D_AlignCenter);
}

static void draw_button(float x, float y, float w, float h, const char *label, bool active, bool pressed) {
    C2D_DrawRectSolid(x, y, 0, w, h, active ? COLOR_ACCENT : pressed ? COLOR_ACTIVE : COLOR_TAB);
    draw_text(label, x + w / 2, y + (h - TEXT_H * 1.2f) / 2, 0.6f, active ? COLOR_BG : COLOR_TEXT, C2D_AlignCenter);
}

static void draw_audio(void) {
    draw_text("Sound plays on", PAD, TAB_H + 10, 0.45f, COLOR_DIM, 0);
    draw_button(PLAY_X, SEGMENT_Y, BUTTON_W, SEGMENT_H, "TV", !audio_wanted, touch_target == TOUCH_TV);
    draw_button(STOP_X, SEGMENT_Y, BUTTON_W, SEGMENT_H, "3DS", audio_wanted, touch_target == TOUCH_3DS);

    char status[128];
    u32 color = COLOR_DIM;
    if (!audio_available) {
        snprintf(status, sizeof status, "3DS audio needs sdmc:/3ds/dspfirm.cdc");
        color = COLOR_ERROR;
    } else if (!audio_wanted) {
        snprintf(status, sizeof status, "Sound is playing on the TV");
    } else {
        switch (audio_state()) {
            case AUDIO_CONNECTING: snprintf(status, sizeof status, "Connecting to the Pi..."); break;
            case AUDIO_BUFFERING: snprintf(status, sizeof status, "Buffering..."); break;
            case AUDIO_PLAYING:
                snprintf(status, sizeof status, "Playing on the 3DS (%d ms buffered)", audio_buffered_ms());
                break;
            case AUDIO_FAILED:
                snprintf(status, sizeof status, "%s. Retrying...", audio_error());
                color = COLOR_ERROR;
                break;
            case AUDIO_OFF: status[0] = '\0'; break;
        }
    }
    C2D_Text status_text;
    parse_fitted(&status_text, frame_buf, status, 0.45f, SCREEN_W - 2 * PAD);
    C2D_DrawText(&status_text, C2D_WithColor | C2D_AlignCenter, SCREEN_W / 2, 110, 0, 0.45f, 0.45f, color);

    draw_text("Lip sync", PAD, 142, 0.45f, COLOR_DIM, 0);
    draw_button(MINUS_X, STEP_Y, STEP_W, STEP_H, "-", false, touch_target == TOUCH_MINUS);
    draw_button(PLUS_X, STEP_Y, STEP_W, STEP_H, "+", false, touch_target == TOUCH_PLUS);
    char value[16];
    snprintf(value, sizeof value, "%d ms", audio_delay);
    draw_text(value, SCREEN_W / 2, STEP_Y + 8, 0.7f, COLOR_TEXT, C2D_AlignCenter);
    draw_text("Raise it if voices come after lips move", SCREEN_W / 2, 214, 0.42f, COLOR_DIM, C2D_AlignCenter);
}

static void retry_audio(void) {
    if (!audio_wanted || audio_state() != AUDIO_FAILED) {
        audio_retry_timer = 0;
        return;
    }
    if (++audio_retry_timer >= AUDIO_RETRY_FRAMES) {
        audio_retry_timer = 0;
        audio_start(kodi_host);
    }
}

static void draw_tabs(void) {
    float w = SCREEN_W / TAB_COUNT;
    for (int i = 0; i < TAB_COUNT; i++) {
        C2D_DrawRectSolid(i * w, 0, 0, w, TAB_H, i == (int)tab ? COLOR_ACTIVE : COLOR_TAB);
        C2D_DrawText(&tab_texts[i], C2D_WithColor | C2D_AlignCenter, i * w + w / 2, (TAB_H - TEXT_H) / 2, 0,
                     TEXT_SCALE, TEXT_SCALE, COLOR_TEXT);
    }
}

static void parse_static(C2D_Text *text, const char *str) {
    C2D_TextParse(text, static_buf, str);
    C2D_TextOptimize(text);
}

static void draw_pill(float x, float y, float w, float h, u32 color) {
    float r = h / 2;
    C2D_DrawRectSolid(x + r, y, 0, w - h, h, color);
    C2D_DrawCircleSolid(x + r, y + r, 0, r, color);
    C2D_DrawCircleSolid(x + w - r, y + r, 0, r, color);
}

static void draw_top(u32 held) {
    C2D_DrawRectangle(0, 0, 0, TOP_W, HEADER_H, COLOR_ACTIVE, COLOR_HEADER_DARK, COLOR_ACTIVE, COLOR_HEADER_DARK);
    C2D_DrawText(&title_text, C2D_WithColor, 16, 7, 0, 0.8f, 0.8f, COLOR_TEXT);
    C2D_DrawText(&host_text, C2D_WithColor | C2D_AlignRight, TOP_W - 16, 13, 0, TEXT_SCALE, TEXT_SCALE, COLOR_DIM);

    for (int i = 0; i < HINT_COUNT; i++) {
        float x = i < HINT_ROWS ? 24 : 214;
        float y = HINT_Y + (i % HINT_ROWS) * HINT_STEP;
        bool lit = held & HINTS[i].keys;
        draw_pill(x, y, PILL_W, PILL_H, lit ? COLOR_ACCENT : COLOR_TAB);
        C2D_DrawText(&hint_buttons[i], C2D_WithColor | C2D_AlignCenter, x + PILL_W / 2, y + 3, 0, 0.45f, 0.45f,
                     lit ? COLOR_BG : COLOR_TEXT);
        C2D_DrawText(&hint_actions[i], C2D_WithColor, x + PILL_W + 12, y + 2, 0, 0.55f, 0.55f,
                     lit ? COLOR_TEXT : COLOR_DIM);
    }

    if (error_frames > 0) {
        error_frames--;
        C2D_DrawRectSolid(0, FOOTER_Y, 0, TOP_W, SCREEN_H - FOOTER_Y, COLOR_ERROR_BG);
        C2D_Text text;
        parse_fitted(&text, frame_buf, error_message, TEXT_SCALE, TOP_W - 2 * PAD);
        C2D_DrawText(&text, C2D_WithColor | C2D_AlignCenter, TOP_W / 2, FOOTER_Y + 6, 0, TEXT_SCALE, TEXT_SCALE,
                     COLOR_ERROR);
    }
}

void ui_init(const char *host, bool audio_ok) {
    snprintf(kodi_host, sizeof kodi_host, "%s", host);
    audio_available = audio_ok;
    load_delay();

    C3D_Init(C3D_DEFAULT_CMDBUF_SIZE);
    C2D_Init(C2D_DEFAULT_MAX_OBJECTS);
    C2D_Prepare();
    top_screen = C2D_CreateScreenTarget(GFX_TOP, GFX_LEFT);
    screen = C2D_CreateScreenTarget(GFX_BOTTOM, GFX_LEFT);

    static_buf = C2D_TextBufNew(512);
    frame_buf = C2D_TextBufNew(1024);
    scratch_buf = C2D_TextBufNew(256);
    for (int i = 0; i < TAB_COUNT; i++) parse_static(&tab_texts[i], TAB_NAMES[i]);
    for (int i = 0; i < HINT_COUNT; i++) {
        parse_static(&hint_buttons[i], HINTS[i].button);
        parse_static(&hint_actions[i], HINTS[i].action);
    }
    parse_static(&title_text, "no porn allowed");
    char host_label[96];
    snprintf(host_label, sizeof host_label, "Kodi at %s", host);
    parse_static(&host_text, host_label);
}

void ui_exit(void) {
    reset_page(&movies);
    for (int i = 0; i < 3; i++) reset_page(&shows[i]);
    C2D_TextBufDelete(static_buf);
    C2D_TextBufDelete(frame_buf);
    C2D_TextBufDelete(scratch_buf);
    C2D_Fini();
    C3D_Fini();
}

void ui_frame(u32 down, u32 held, u32 up) {
    handle_touch(down, held, up);
    poll_now_playing();
    retry_audio();

    C2D_TextBufClear(frame_buf);
    C3D_FrameBegin(C3D_FRAME_SYNCDRAW);
    C2D_TargetClear(top_screen, COLOR_BG);
    C2D_SceneBegin(top_screen);
    draw_top(held);

    C2D_TargetClear(screen, COLOR_BG);
    C2D_SceneBegin(screen);
    ListPage *page = current_page();
    if (page) {
        draw_list(page);
    } else if (tab == TAB_PLAYING) {
        draw_playing();
    } else {
        draw_audio();
    }
    draw_tabs();
    C3D_FrameEnd(0);

    // Loads run after the frame so "Loading..." is on screen while the request blocks.
    if (pending_load) {
        ListPage *p = pending_load;
        pending_load = NULL;
        load_page(p);
    }
}

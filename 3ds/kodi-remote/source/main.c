#include <3ds.h>
#include <malloc.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "audio.h"
#include "kodi.h"
#include "ui.h"

#define CONFIG_PATH "sdmc:/3ds/kodi-remote.cfg"
#define SOC_BUFFERSIZE 0x100000

typedef struct {
    u32 keys;
    const char *label;
    const char *action;  // Input.ExecuteAction action name, NULL for Input.Home
    bool repeat;
} Binding;

static const Binding BINDINGS[] = {
    {KEY_UP, "Up", "up", true},
    {KEY_DOWN, "Down", "down", true},
    {KEY_LEFT, "Left", "left", true},
    {KEY_RIGHT, "Right", "right", true},
    {KEY_A, "Select", "select", false},
    {KEY_B, "Back", "back", false},
    {KEY_X, "Menu", "contextmenu", false},
    {KEY_Y, "Info", "info", false},
    {KEY_START, "Play/Pause", "playpause", false},
    {KEY_SELECT, "Home", NULL, false},
    {KEY_L | KEY_ZL, "Volume down", "volumedown", true},
    {KEY_R | KEY_ZR, "Volume up", "volumeup", true},
};

static bool load_config(KodiConfig *c) {
    *c = (KodiConfig){.port = 8080, .user = "kodi"};
    FILE *f = fopen(CONFIG_PATH, "r");
    if (!f) return false;

    char line[256];
    while (fgets(line, sizeof line, f)) {
        line[strcspn(line, "\r\n")] = '\0';
        char *eq = strchr(line, '=');
        if (line[0] == '#' || !eq) continue;
        *eq = '\0';
        const char *key = line, *value = eq + 1;

        if (!strcmp(key, "host")) snprintf(c->host, sizeof c->host, "%s", value);
        else if (!strcmp(key, "port")) c->port = atoi(value);
        else if (!strcmp(key, "user")) snprintf(c->user, sizeof c->user, "%s", value);
        else if (!strcmp(key, "password")) snprintf(c->password, sizeof c->password, "%s", value);
    }
    fclose(f);
    return c->host[0] && c->password[0];
}

// Keeps only the Circle Pad's dominant axis so a diagonal doesn't send two directions.
static u32 dominant_cpad_axis(u32 keys) {
    if (!(keys & (KEY_CPAD_UP | KEY_CPAD_DOWN | KEY_CPAD_LEFT | KEY_CPAD_RIGHT))) return keys;
    circlePosition pos;
    hidCircleRead(&pos);
    return keys & ~(abs(pos.dx) > abs(pos.dy) ? (KEY_CPAD_UP | KEY_CPAD_DOWN) : (KEY_CPAD_LEFT | KEY_CPAD_RIGHT));
}

static void wait_for_start(void) {
    while (aptMainLoop()) {
        hidScanInput();
        if (hidKeysDown() & KEY_START) break;
        gspWaitForVBlank();
    }
}

int main(void) {
    gfxInitDefault();

    KodiConfig config;
    if (!load_config(&config)) {
        consoleInit(GFX_TOP, NULL);
        printf("Missing host or password in\n%s\n\nPress START to exit.\n", CONFIG_PATH);
        wait_for_start();
        gfxExit();
        return 0;
    }
    kodi_init(&config);
    ui_init(config.host, audio_init());

    u32 *soc_buf = memalign(0x1000, SOC_BUFFERSIZE);
    Result rc = socInit(soc_buf, SOC_BUFFERSIZE);
    if (R_FAILED(rc)) {
        ui_error("Network init failed: 0x%08lX", rc);
    } else if (gethostid() == 0) {
        ui_error("3DS is not connected to Wi-Fi");
    }
    // Counted in frames: first repeat after ~1/3 s, then every 1/10 s.
    hidSetRepeatParameters(20, 6);

    while (aptMainLoop()) {
        hidScanInput();
        u32 down = hidKeysDown();
        u32 held = hidKeysHeld();
        u32 up = hidKeysUp();
        u32 pressed = dominant_cpad_axis(down);
        u32 repeated = dominant_cpad_axis(hidKeysDownRepeat());

        for (size_t i = 0; i < sizeof BINDINGS / sizeof BINDINGS[0]; i++) {
            const Binding *b = &BINDINGS[i];
            if (!((b->repeat ? repeated : pressed) & b->keys)) continue;
            bool ok = b->action ? kodi_action(b->action) : kodi_home();
            if (!ok) ui_error("%s failed: %s", b->label, kodi_error());
        }

        ui_frame(down, held, up);
    }

    ui_exit();
    audio_exit();
    socExit();
    free(soc_buf);
    gfxExit();
    return 0;
}

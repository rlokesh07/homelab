// Exercises kodi.c against fake_kodi.py. Run via test/run.sh.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "kodi.h"

static int failures;

#define CHECK(cond)                                                                       \
    do {                                                                                  \
        if (!(cond)) {                                                                    \
            printf("FAIL %s:%d: %s (kodi_error: %s)\n", __FILE__, __LINE__, #cond, kodi_error()); \
            failures++;                                                                   \
        }                                                                                 \
    } while (0)

static KodiConfig config_for(int port, const char *password) {
    KodiConfig c = {.port = port};
    strcpy(c.host, "127.0.0.1");
    strcpy(c.user, "kodi");
    strcpy(c.password, password);
    return c;
}

int main(int argc, char **argv) {
    int port = atoi(argv[1]);
    KodiConfig config = config_for(port, "secret");
    kodi_init(&config);

    KodiList list;
    CHECK(kodi_movies(&list));
    CHECK(list.count == 1502);
    CHECK(list.items[0].id == 1 && !strcmp(list.items[0].label, "Amélie (2001)"));
    CHECK(list.items[1].id == 2 && strlen(list.items[1].label) < sizeof list.items[1].label);
    CHECK(!strstr(list.items[1].label, "(0)"));
    CHECK(list.items[1501].id == 1599);
    kodi_list_free(&list);
    CHECK(list.items == NULL && list.count == 0);

    CHECK(kodi_shows(&list));
    CHECK(list.count == 1 && list.items[0].id == 7 && !strcmp(list.items[0].label, "Severance"));
    kodi_list_free(&list);

    CHECK(kodi_seasons(7, &list));
    CHECK(list.count == 2 && list.items[1].id == 2 && !strcmp(list.items[1].label, "Season 2"));
    kodi_list_free(&list);

    CHECK(kodi_episodes(7, 2, &list));
    CHECK(list.count == 2 && list.items[0].id == 70);
    kodi_list_free(&list);

    CHECK(kodi_seasons(999, &list));
    CHECK(list.count == 0);
    kodi_list_free(&list);

    KodiNowPlaying now;
    CHECK(kodi_now_playing(&now));
    CHECK(!now.active);

    CHECK(kodi_play_episode(70));
    CHECK(kodi_now_playing(&now));
    CHECK(now.active && now.playerid == 1 && now.paused);
    CHECK(now.time == 12 * 60 + 3 && now.total == 3720);
    CHECK(now.percentage > 25 && now.percentage < 26);
    CHECK(!strcmp(now.title, "Severance - Hello, Ms. Cobel"));

    CHECK(kodi_seek(1, 42.5));
    CHECK(kodi_play_pause(1));
    CHECK(kodi_stop(1));
    CHECK(kodi_play_movie(2));
    CHECK(kodi_action("volumeup"));
    CHECK(kodi_home());

    CHECK(utf8_fit("Amélie", 3) == 2);  // don't split the 2-byte é
    CHECK(utf8_fit("Amélie", 4) == 4);
    CHECK(utf8_fit("abc", 10) == 3);

    KodiConfig bad = config_for(port, "wrong");
    kodi_init(&bad);
    CHECK(!kodi_home() && !strcmp(kodi_error(), "wrong user/password"));

    KodiConfig closed = config_for(1, "secret");
    kodi_init(&closed);
    CHECK(!kodi_home() && !strncmp(kodi_error(), "connect failed", 14));

    printf(failures ? "%d FAILED\n" : "all passed\n", failures);
    return failures != 0;
}

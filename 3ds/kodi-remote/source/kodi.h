#pragma once
#include <stdbool.h>
#include <stddef.h>

typedef struct {
    char host[64];
    int port;
    char user[64];
    char password[128];
} KodiConfig;

typedef enum { KODI_MOVIES, KODI_SHOWS, KODI_SEASONS, KODI_EPISODES } KodiListKind;

typedef struct {
    int id;  // movieid, tvshowid, season number or episodeid, depending on the list
    char label[128];
} KodiItem;

typedef struct {
    KodiListKind kind;
    KodiItem *items;
    int count;
} KodiList;

typedef struct {
    bool active;
    int playerid;
    bool paused;
    char title[160];
    double percentage;
    int time, total;  // seconds
} KodiNowPlaying;

void kodi_init(const KodiConfig *config);

// All calls return false on failure; kodi_error() then describes why.
const char *kodi_error(void);

bool kodi_action(const char *action);  // Input.ExecuteAction
bool kodi_home(void);

bool kodi_movies(KodiList *out);
bool kodi_shows(KodiList *out);
bool kodi_seasons(int tvshowid, KodiList *out);
bool kodi_episodes(int tvshowid, int season, KodiList *out);
void kodi_list_free(KodiList *list);

bool kodi_play_movie(int movieid);
bool kodi_play_episode(int episodeid);

bool kodi_now_playing(KodiNowPlaying *out);
bool kodi_play_pause(int playerid);
bool kodi_stop(int playerid);
bool kodi_seek(int playerid, double percentage);

// Length of the longest prefix of s that is at most max_bytes and doesn't split a UTF-8 character.
size_t utf8_fit(const char *s, size_t max_bytes);

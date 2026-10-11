#include "kodi.h"

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <poll.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "cJSON.h"

#define TIMEOUT_MS 5000

static KodiConfig config;
static char auth[256];
static char error_text[128];

static bool fail(const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    vsnprintf(error_text, sizeof error_text, fmt, args);
    va_end(args);
    return false;
}

const char *kodi_error(void) { return error_text; }

size_t utf8_fit(const char *s, size_t max_bytes) {
    size_t len = strlen(s);
    if (len <= max_bytes) return len;
    while (max_bytes > 0 && ((unsigned char)s[max_bytes] & 0xC0) == 0x80) max_bytes--;
    return max_bytes;
}

static void base64(const char *in, char *out) {
    static const char table[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    size_t len = strlen(in);
    for (size_t i = 0; i < len; i += 3) {
        uint32_t n = (uint8_t)in[i] << 16;
        if (i + 1 < len) n |= (uint8_t)in[i + 1] << 8;
        if (i + 2 < len) n |= (uint8_t)in[i + 2];
        *out++ = table[(n >> 18) & 63];
        *out++ = table[(n >> 12) & 63];
        *out++ = i + 1 < len ? table[(n >> 6) & 63] : '=';
        *out++ = i + 2 < len ? table[n & 63] : '=';
    }
    *out = '\0';
}

void kodi_init(const KodiConfig *c) {
    config = *c;
    char credentials[192];
    snprintf(credentials, sizeof credentials, "%s:%s", config.user, config.password);
    base64(credentials, auth);
}

static int open_connection(void) {
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons(config.port)};
    if (!inet_aton(config.host, &addr.sin_addr)) {
        fail("host is not an IP address");
        return -1;
    }
    int sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) {
        fail("socket failed: %s (%d)", strerror(errno), errno);
        return -1;
    }
    if (connect(sock, (struct sockaddr *)&addr, sizeof addr) < 0) {
        fail("connect failed: %s (%d)", strerror(errno), errno);
        close(sock);
        return -1;
    }
    return sock;
}

static bool send_all(int sock, const char *data, size_t len) {
    while (len > 0) {
        ssize_t n = send(sock, data, len, 0);
        if (n <= 0) return fail("send failed: %s (%d)", strerror(errno), errno);
        data += n;
        len -= n;
    }
    return true;
}

// Returns the malloc'd response body, or NULL.
static char *http_post(const char *body) {
    char header[512];
    // HTTP/1.0 so the response is never chunked.
    int header_len = snprintf(header, sizeof header,
                              "POST /jsonrpc HTTP/1.0\r\n"
                              "Host: %s:%d\r\n"
                              "Authorization: Basic %s\r\n"
                              "Content-Type: application/json\r\n"
                              "Content-Length: %d\r\n"
                              "\r\n",
                              config.host, config.port, auth, (int)strlen(body));

    int sock = open_connection();
    if (sock < 0) return NULL;
    if (!send_all(sock, header, header_len) || !send_all(sock, body, strlen(body))) {
        close(sock);
        return NULL;
    }

    size_t cap = 4096, len = 0;
    char *buf = malloc(cap);
    struct pollfd p = {.fd = sock, .events = POLLIN};
    while (buf && poll(&p, 1, TIMEOUT_MS) == 1) {
        if (len + 1 >= cap) {
            char *bigger = realloc(buf, cap *= 2);
            if (!bigger) {
                free(buf);
                buf = NULL;
                break;
            }
            buf = bigger;
        }
        ssize_t n = recv(sock, buf + len, cap - 1 - len, 0);
        if (n <= 0) break;
        len += n;
    }
    close(sock);
    if (!buf) {
        fail("out of memory");
        return NULL;
    }
    buf[len] = '\0';

    int status = 0;
    char *body_start = strstr(buf, "\r\n\r\n");
    if (sscanf(buf, "HTTP/1.%*d %d", &status) != 1 || !body_start) {
        free(buf);
        fail(len ? "bad response from Kodi" : "no response from Kodi");
        return NULL;
    }
    if (status != 200) {
        free(buf);
        fail(status == 401 ? "wrong user/password" : "HTTP error %d", status);
        return NULL;
    }
    body_start += 4;
    memmove(buf, body_start, len - (body_start - buf) + 1);
    return buf;
}

// Calls a JSON-RPC method, taking ownership of params (may be NULL).
// Returns the parsed response for the caller to cJSON_Delete, with *result pointing into it.
static cJSON *call(const char *method, cJSON *params, cJSON **result) {
    cJSON *request = cJSON_CreateObject();
    cJSON_AddStringToObject(request, "jsonrpc", "2.0");
    cJSON_AddNumberToObject(request, "id", 1);
    cJSON_AddStringToObject(request, "method", method);
    if (params) cJSON_AddItemToObject(request, "params", params);
    char *body = cJSON_PrintUnformatted(request);
    cJSON_Delete(request);
    if (!body) {
        fail("out of memory");
        return NULL;
    }

    char *response = http_post(body);
    free(body);
    if (!response) return NULL;

    cJSON *root = cJSON_Parse(response);
    free(response);
    if (!root) {
        fail("bad response from Kodi");
        return NULL;
    }
    cJSON *error = cJSON_GetObjectItem(root, "error");
    if (error) {
        const char *message = cJSON_GetStringValue(cJSON_GetObjectItem(error, "message"));
        fail("Kodi: %s", message ? message : "error");
        cJSON_Delete(root);
        return NULL;
    }
    *result = cJSON_GetObjectItem(root, "result");
    if (!*result) {
        fail("bad response from Kodi");
        cJSON_Delete(root);
        return NULL;
    }
    return root;
}

static bool call_simple(const char *method, cJSON *params) {
    cJSON *result;
    cJSON *root = call(method, params, &result);
    cJSON_Delete(root);
    return root != NULL;
}

static int get_int(const cJSON *object, const char *key, int fallback) {
    const cJSON *value = cJSON_GetObjectItem(object, key);
    return cJSON_IsNumber(value) ? value->valueint : fallback;
}

static cJSON *params_printf(const char *fmt, ...) {
    char json[256];
    va_list args;
    va_start(args, fmt);
    vsnprintf(json, sizeof json, fmt, args);
    va_end(args);
    return cJSON_Parse(json);
}

bool kodi_action(const char *action) {
    cJSON *params = cJSON_CreateObject();
    cJSON_AddStringToObject(params, "action", action);
    return call_simple("Input.ExecuteAction", params);
}

bool kodi_home(void) { return call_simple("Input.Home", NULL); }

static bool fetch_list(KodiListKind kind, const char *method, cJSON *params, const char *array_name,
                       const char *id_name, KodiList *out) {
    *out = (KodiList){.kind = kind};
    cJSON *result;
    cJSON *root = call(method, params, &result);
    if (!root) return false;

    cJSON *array = cJSON_GetObjectItem(result, array_name);
    int count = cJSON_GetArraySize(array);
    if (count > 0 && !(out->items = calloc(count, sizeof(KodiItem)))) {
        cJSON_Delete(root);
        return fail("out of memory");
    }

    cJSON *entry;
    cJSON_ArrayForEach(entry, array) {
        KodiItem *item = &out->items[out->count++];
        item->id = get_int(entry, id_name, -1);
        const char *label = cJSON_GetStringValue(cJSON_GetObjectItem(entry, "label"));
        int year = get_int(entry, "year", 0);
        if (!label) label = "";
        int n = (int)utf8_fit(label, sizeof item->label - 8);
        if (kind == KODI_MOVIES && year > 0) {
            snprintf(item->label, sizeof item->label, "%.*s (%d)", n, label, year);
        } else {
            snprintf(item->label, sizeof item->label, "%.*s", n, label);
        }
    }
    cJSON_Delete(root);
    return true;
}

bool kodi_movies(KodiList *out) {
    return fetch_list(KODI_MOVIES, "VideoLibrary.GetMovies",
                      params_printf("{\"properties\":[\"year\"],\"sort\":{\"method\":\"title\",\"ignorearticle\":true}}"),
                      "movies", "movieid", out);
}

bool kodi_shows(KodiList *out) {
    return fetch_list(KODI_SHOWS, "VideoLibrary.GetTVShows",
                      params_printf("{\"sort\":{\"method\":\"title\",\"ignorearticle\":true}}"), "tvshows",
                      "tvshowid", out);
}

bool kodi_seasons(int tvshowid, KodiList *out) {
    return fetch_list(KODI_SEASONS, "VideoLibrary.GetSeasons",
                      params_printf("{\"tvshowid\":%d,\"properties\":[\"season\"]}", tvshowid), "seasons", "season",
                      out);
}

bool kodi_episodes(int tvshowid, int season, KodiList *out) {
    return fetch_list(KODI_EPISODES, "VideoLibrary.GetEpisodes",
                      params_printf("{\"tvshowid\":%d,\"season\":%d,\"sort\":{\"method\":\"episode\"}}", tvshowid,
                                    season),
                      "episodes", "episodeid", out);
}

void kodi_list_free(KodiList *list) {
    free(list->items);
    *list = (KodiList){.kind = list->kind};
}

bool kodi_play_movie(int movieid) {
    return call_simple("Player.Open", params_printf("{\"item\":{\"movieid\":%d}}", movieid));
}

bool kodi_play_episode(int episodeid) {
    return call_simple("Player.Open", params_printf("{\"item\":{\"episodeid\":%d}}", episodeid));
}

static int seconds(cJSON *time) {
    return get_int(time, "hours", 0) * 3600 + get_int(time, "minutes", 0) * 60 + get_int(time, "seconds", 0);
}

bool kodi_now_playing(KodiNowPlaying *out) {
    *out = (KodiNowPlaying){0};
    cJSON *result;
    cJSON *root = call("Player.GetActivePlayers", NULL, &result);
    if (!root) return false;
    cJSON *player = cJSON_GetArrayItem(result, 0);
    if (player) {
        out->active = true;
        out->playerid = get_int(player, "playerid", 1);
    }
    cJSON_Delete(root);
    if (!out->active) return true;

    root = call("Player.GetProperties",
                params_printf("{\"playerid\":%d,\"properties\":[\"percentage\",\"time\",\"totaltime\",\"speed\"]}",
                              out->playerid),
                &result);
    if (!root) return false;
    cJSON *percentage = cJSON_GetObjectItem(result, "percentage");
    out->percentage = cJSON_IsNumber(percentage) ? percentage->valuedouble : 0;
    out->time = seconds(cJSON_GetObjectItem(result, "time"));
    out->total = seconds(cJSON_GetObjectItem(result, "totaltime"));
    out->paused = get_int(result, "speed", 1) == 0;
    cJSON_Delete(root);

    root = call("Player.GetItem", params_printf("{\"playerid\":%d,\"properties\":[\"showtitle\"]}", out->playerid),
                &result);
    if (!root) return false;
    cJSON *item = cJSON_GetObjectItem(result, "item");
    const char *label = cJSON_GetStringValue(cJSON_GetObjectItem(item, "label"));
    const char *show = cJSON_GetStringValue(cJSON_GetObjectItem(item, "showtitle"));
    if (show && *show) {
        snprintf(out->title, sizeof out->title, "%s - %s", show, label ? label : "");
    } else {
        snprintf(out->title, sizeof out->title, "%s", label ? label : "");
    }
    out->title[utf8_fit(out->title, sizeof out->title - 2)] = '\0';
    cJSON_Delete(root);
    return true;
}

bool kodi_play_pause(int playerid) {
    return call_simple("Player.PlayPause", params_printf("{\"playerid\":%d}", playerid));
}

bool kodi_stop(int playerid) { return call_simple("Player.Stop", params_printf("{\"playerid\":%d}", playerid)); }

bool kodi_seek(int playerid, double percentage) {
    return call_simple("Player.Seek",
                       params_printf("{\"playerid\":%d,\"value\":{\"percentage\":%.2f}}", playerid, percentage));
}

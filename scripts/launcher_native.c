/* EduPACS 教学病例库 —— 原生启动器（C + Win32 API）
 *
 * 目的：替代 PowerShell/WinForms 版启动器（scripts/launcher.ps1）——
 *       原生 GUI 启动即开（毫秒级），健康检测在后台线程执行，界面不卡顿。
 * 风格：赛博朋克 HUD——深色底、霓虹青/品红、网格背景、扫描线动画、自绘按钮。
 *
 * 功能：
 *   - 状态灯：Orthanc(8042, 带认证) / 教学系统(8000)，每 2 秒后台刷新
 *   - 一键启动：按需拉起 Orthanc 与 uvicorn（CREATE_NO_WINDOW），就绪后自动打开浏览器
 *   - 一键停止：安全停止——只结束命令行可证明属于本项目的进程（路径含项目根或 app.backend.main），
 *               不按端口+进程名盲杀，不误杀同机其它服务
 *   - 快捷按钮：打开教学系统 / 打开 Orthanc 后台（浏览器优先 Edge/Chrome 硬编码路径，兼容精简系统）
 *
 * 命令行模式（供脚本调用，输出 UTF-8）：
 *   EduPACS启动器.exe --status    查看状态，退出码 0=两服务就绪，1=有服务未就绪
 *   EduPACS启动器.exe --start     拉起服务并等待就绪（最长 50 秒）
 *   EduPACS启动器.exe --stop      安全停止
 *
 * 编译（zig cc = clang，纯静态链接，无 DLL 依赖；GUI 子系统无控制台窗口）：
 *   zig rc scripts/launcher.rc scripts/launcher.res      # 应用图标资源（引用 app/static/favicon.ico）
 *   zig cc scripts/launcher_native.c scripts/launcher.res -O2 -s -static \
 *        -mcpu=baseline -Wl,--subsystem,windows -o EduPACS启动器.exe \
 *        -lwininet -liphlpapi -lshell32 -lgdi32 -luser32
 *   注意：未紧跟参数时 "-Wl,--subsystem,windows" 在 PowerShell 里需整体加引号。
 *   【重要】必须加 -mcpu=baseline：否则较新 zig 会内联生成 AVX-512 指令，
 *   在不支持 AVX-512 的老 CPU（如 i5-3340M）上 --stop 路径会抛 0xc000001d 非法指令崩溃。
 *   baseline = x86-64 基线 SSE2，任何 amd64 机器都能运行。
 */

#define UNICODE
#define _UNICODE
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <windows.h>
#include <wininet.h>
#include <iphlpapi.h>
#include <tlhelp32.h>
#include <shellapi.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---------------------------------------------------------------- 全局 */
#define WM_APP_STATUS   (WM_APP + 1)   /* wParam=Orthanc探测码 lParam=App探测码 */
#define WM_APP_DONE     (WM_APP + 2)   /* wParam=ok|job<<16  lParam=堆上 wchar_t* 文本 */

#define IDI_APPICON     200   /* 应用图标资源（见 scripts/launcher.rc） */

#define IDC_TITLE       101
#define IDC_ST_O        102
#define IDC_ST_A        103
#define IDC_BTN_START   104
#define IDC_BTN_STOP    105
#define IDC_BTN_APP     106
#define IDC_BTN_O       107
#define IDC_ADDR        108
#define IDC_TIP         109
#define IDC_STATUS      110

#define JOB_REFRESH 0
#define JOB_START   1
#define JOB_STOP    2

#define TIMER_REFRESH 1
#define TIMER_SCAN    2               /* 扫描线动画 */
#define TIMER_TRAK    3               /* hover 跟踪（一次性 TrackMouseEvent） */

static HWND  g_hwnd = NULL;
static WCHAR g_root[MAX_PATH];              /* 项目根（exe 所在目录） */
static char  g_root_u8[MAX_PATH * 3];       /* 根路径 UTF-8（命令行匹配用） */
static char  g_o_user[128];                 /* Orthanc 服务账号（orthanc.json） */
static char  g_o_pass[256];
static int   g_st_o = -1, g_st_a = -1;      /* -1 检查中 / 0 未运行 / 1 正常 / 2 认证不匹配 */
static int   g_busy = 0;                    /* 仅 UI 线程读写：1=启动/停止任务进行中 */
static HANDLE g_ev = NULL;                  /* 自动复位事件：唤醒后台线程 */
static CRITICAL_SECTION g_cs;               /* 保护 g_job */
static int   g_job = -1;                    /* 待执行任务，-1 无 */
static int   g_scan_y = 0;                 /* 扫描线 Y（动画） */
static int   g_hover = 0;                  /* 当前 hover 的按钮 ID（0=无） */

/* 赛博朋克配色 */
#define BG_TOP     RGB(5, 9, 18)
#define BG_BOTTOM  RGB(13, 22, 42)
#define GRID_COLOR RGB(20, 42, 70)
#define NEON_CYAN  RGB(0, 240, 208)
#define CYAN_DIM   RGB(0, 168, 146)
#define CYAN_FAINT RGB(2, 120, 104)
#define NEON_MAG   RGB(255, 45, 149)
#define MAG_DIM    RGB(168, 50, 100)
#define RED_OFF    RGB(224, 74, 74)
#define AMBER_WARN RGB(255, 168, 40)
#define TXT_DIM    RGB(120, 158, 186)
#define TXT_FAINT  RGB(78, 104, 134)
#define BTN_BG     RGB(9, 16, 30)
#define BTN_HOVER  RGB(18, 36, 62)

/* UTF-8 -> UTF-16（UI 线程专用轮转缓冲） */
static wchar_t *u8(const char *s)
{
    static wchar_t buf[8][1024];   /* 仅 UI 线程调用；worker 用 u8_worker */
    static int idx = 0;
    wchar_t *p = buf[idx];
    idx = (idx + 1) & 7;
    if (!MultiByteToWideChar(CP_UTF8, 0, s, -1, p, 1024))
        p[0] = 0;
    return p;
}

/* UTF-8 -> UTF-16（后台 worker 线程专用，与 UI 线程互不竞争） */
static wchar_t *u8_worker(const char *s)
{
    static wchar_t wbuf[2048];     /* 仅 worker 线程调用 */
    if (!MultiByteToWideChar(CP_UTF8, 0, s, -1, wbuf, 2048))
        wbuf[0] = 0;
    return wbuf;
}

static wchar_t *wcs_dup(const wchar_t *s)
{
    size_t n = wcslen(s) + 1;
    wchar_t *p = (wchar_t *)malloc(n * sizeof(wchar_t));
    if (p) memcpy(p, s, n * sizeof(wchar_t));
    return p;
}

static void w2u8(const wchar_t *w, char *out, int outlen)
{
    if (!WideCharToMultiByte(CP_UTF8, 0, w, -1, out, outlen, NULL, NULL))
        out[0] = 0;
}

/* ASCII 不区分大小写的子串匹配（>127 字节按原值比较，兼容中文路径） */
static const char *ci_str(const char *hay, const char *needle)
{
    if (!needle || !*needle) return hay;
    for (; *hay; hay++) {
        const char *h = hay, *n = needle;
        while (*h && *n) {
            char a = *h, b = *n;
            if (a >= 'A' && a <= 'Z') a += 32;
            if (b >= 'A' && b <= 'Z') b += 32;
            if (a != b) break;
            h++; n++;
        }
        if (!*n) return hay;
    }
    return NULL;
}

static void b64(const char *in, char *out)
{
    static const char t[] =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    int n = (int)strlen(in), i, j = 0;
    for (i = 0; i + 2 < n; i += 3) {
        unsigned v = ((unsigned char)in[i] << 16) | ((unsigned char)in[i + 1] << 8)
                   | (unsigned char)in[i + 2];
        out[j++] = t[(v >> 18) & 63]; out[j++] = t[(v >> 12) & 63];
        out[j++] = t[(v >> 6) & 63];  out[j++] = t[v & 63];
    }
    if (n - i == 1) {
        unsigned v = (unsigned char)in[i] << 16;
        out[j++] = t[(v >> 18) & 63]; out[j++] = t[(v >> 12) & 63];
        out[j++] = '='; out[j++] = '=';
    } else if (n - i == 2) {
        unsigned v = ((unsigned char)in[i] << 16) | ((unsigned char)in[i + 1] << 8);
        out[j++] = t[(v >> 18) & 63]; out[j++] = t[(v >> 12) & 63];
        out[j++] = t[(v >> 6) & 63];  out[j++] = '=';
    }
    out[j] = 0;
}

/* ---------------------------------------------------------------- 配置加载 */
static void load_config(void)
{
    WCHAR exe[MAX_PATH];
    g_o_user[0] = g_o_pass[0] = 0;
    g_root[0] = 0;
    if (!GetModuleFileNameW(NULL, exe, MAX_PATH))
        return;
    WCHAR *slash = wcsrchr(exe, L'\\');
    if (slash) *slash = 0;
    lstrcpynW(g_root, exe, MAX_PATH);
    w2u8(g_root, g_root_u8, (int)sizeof(g_root_u8));

    /* 读 orthanc.json 的 RegisteredUsers 第一个服务账号（机器管理的 JSON，简单扫描足够） */
    WCHAR pjson[MAX_PATH * 2];
    _snwprintf(pjson, MAX_PATH * 2 - 1, L"%ls\\orthanc\\orthanc.json", g_root);
    FILE *f = _wfopen(pjson, L"rb");
    if (!f) return;
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (sz > 0 && sz < 4 * 1024 * 1024) {
        char *buf = (char *)malloc((size_t)sz + 1);
        if (buf && fread(buf, 1, (size_t)sz, f) == (size_t)sz) {
            buf[sz] = 0;
            const char *ru = strstr(buf, "\"RegisteredUsers\"");
            const char *ob = ru ? strchr(ru, '{') : NULL;
            if (ob) {
                const char *q1 = strchr(ob + 1, '"');
                const char *q2 = q1 ? strchr(q1 + 1, '"') : NULL;
                const char *co = q2 ? strchr(q2, ':') : NULL;
                const char *v1 = co ? strchr(co, '"') : NULL;
                const char *v2 = v1 ? strchr(v1 + 1, '"') : NULL;
                if (q1 && q2 && (q2 - q1 - 1) < (int)sizeof(g_o_user)) {
                    memcpy(g_o_user, q1 + 1, (size_t)(q2 - q1 - 1));
                    g_o_user[q2 - q1 - 1] = 0;
                }
                if (v1 && v2 && (v2 - v1 - 1) < (int)sizeof(g_o_pass)) {
                    memcpy(g_o_pass, v1 + 1, (size_t)(v2 - v1 - 1));
                    g_o_pass[v2 - v1 - 1] = 0;
                }
            }
        }
        free(buf);
    }
    fclose(f);
}

/* ---------------------------------------------------------------- HTTP 探测 */
/* 返回 HTTP 状态码（200/401/...）；连接失败返回 -1 */
static int http_probe(const char *url, int with_auth)
{
    int rc = -1;
    HINTERNET hO = InternetOpenA("EduPACS-Launcher/2.0", INTERNET_OPEN_TYPE_PRECONFIG,
                                 NULL, NULL, 0);
    if (!hO) return -1;
    DWORD t = 2000;
    InternetSetOptionA(hO, INTERNET_OPTION_CONNECT_TIMEOUT, &t, sizeof t);
    InternetSetOptionA(hO, INTERNET_OPTION_RECEIVE_TIMEOUT, &t, sizeof t);
    InternetSetOptionA(hO, INTERNET_OPTION_SEND_TIMEOUT, &t, sizeof t);

    char hdr[1024] = "User-Agent: EduPACS-Launcher\r\n";
    if (with_auth && g_o_user[0]) {
        char pair[512], b64s[768];
        snprintf(pair, sizeof pair, "%s:%s", g_o_user, g_o_pass);
        b64(pair, b64s);
        snprintf(hdr, sizeof hdr, "User-Agent: EduPACS-Launcher\r\nAuthorization: Basic %s\r\n",
                 b64s);
    }
    HINTERNET hU = InternetOpenUrlA(hO, url, hdr, (DWORD)-1,
        INTERNET_FLAG_RELOAD | INTERNET_FLAG_NO_CACHE_WRITE |
        INTERNET_FLAG_NO_AUTO_REDIRECT | INTERNET_FLAG_NO_UI, 0);
    if (hU) {
        DWORD code = 0, l = sizeof code;
        if (HttpQueryInfoA(hU, HTTP_QUERY_STATUS_CODE | HTTP_QUERY_FLAG_NUMBER,
                           &code, &l, NULL))
            rc = (int)code;
        InternetCloseHandle(hU);
    }
    InternetCloseHandle(hO);
    return rc;
}

static int check_orthanc(void) { return http_probe("http://127.0.0.1:8042/system", 1); }
static int check_app(void)     { return http_probe("http://127.0.0.1:8000/api/config", 0); }

/* ---------------------------------------------------------------- 进程识别（安全停止） */
typedef struct { USHORT Length; USHORT MaximumLength; PWSTR Buffer; } MYUNI;
typedef LONG (WINAPI *NTQIP)(HANDLE, ULONG, PVOID, ULONG, PULONG);

/* 读取目标进程命令行并转 UTF-8；失败返回 NULL（不可识别即不动手） */
static char *get_cmdline_u8(DWORD pid)
{
    static NTQIP pNtQIP;
    if (!pNtQIP) {
        HMODULE nt = GetModuleHandleW(L"ntdll.dll");
        if (!nt) return NULL;
        pNtQIP = (NTQIP)(void *)GetProcAddress(nt, "NtQueryInformationProcess");
        if (!pNtQIP) return NULL;
    }
    HANDLE h = OpenProcess(0x1000 /* PROCESS_QUERY_LIMITED_INFORMATION */, FALSE, pid);
    if (!h) return NULL;
    ULONG len = 0;
    char *out = NULL;
    /* 两次调用模式：首次传 NULL 取所需长度，正常返回 STATUS_INFO_LENGTH_MISMATCH(0xC0000004) */
    LONG st = pNtQIP(h, 60 /* ProcessCommandLineInformation */, NULL, 0, &len);
    if ((st == 0 || st == (LONG)0xC0000004) && len > 0) {
        BYTE *buf = (BYTE *)malloc(len);
        if (buf) {
            if (pNtQIP(h, 60, buf, len, &len) == 0 && len >= sizeof(MYUNI)) {
                MYUNI *u = (MYUNI *)buf;
                wchar_t *ws = NULL;
                if (u->Buffer && (BYTE *)u->Buffer >= buf && (BYTE *)u->Buffer < buf + len)
                    ws = u->Buffer;
                else
                    ws = (wchar_t *)(buf + sizeof(MYUNI));
                int wlen = u->Length / (int)sizeof(wchar_t);
                int need = WideCharToMultiByte(CP_UTF8, 0, ws, wlen, NULL, 0, NULL, NULL);
                if (need > 0 && need < 32000) {
                    out = (char *)malloc((size_t)need + 1);
                    if (out) {
                        WideCharToMultiByte(CP_UTF8, 0, ws, wlen, out, need, NULL, NULL);
                        out[need] = 0;
                    }
                }
            }
            free(buf);
        }
    }
    CloseHandle(h);
    return out;
}

#define MAXPROCS 64
typedef struct { DWORD pid; char exe[64]; char cmd[2048]; } PROCMATCH;
static PROCMATCH g_match[MAXPROCS];
static int g_nmatch = 0;

/* 枚举 Orthanc/python 进程；命令行能证明属于本项目（路径含项目根或 app.backend.main）才记入 */
static void find_project_procs(void)
{
    g_nmatch = 0;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return;
    PROCESSENTRY32W pe;
    pe.dwSize = sizeof pe;
    if (Process32FirstW(snap, &pe)) {
        do {
            int isO = lstrcmpiW(pe.szExeFile, L"Orthanc.exe") == 0;
            int isP = lstrcmpiW(pe.szExeFile, L"python.exe") == 0 ||
                      lstrcmpiW(pe.szExeFile, L"pythonw.exe") == 0;
            if (!isO && !isP) continue;
            char *cl = get_cmdline_u8(pe.th32ProcessID);
            if (!cl) continue;              /* 命令行不可识别 → 不动手 */
            for (char *q = cl; *q; q++)
                if (*q == '/') *q = '\\';
            int ours = (ci_str(cl, g_root_u8) != NULL) ||
                       (ci_str(cl, "app.backend.main") != NULL);
            if (ours && g_nmatch < MAXPROCS) {
                g_match[g_nmatch].pid = pe.th32ProcessID;
                w2u8(pe.szExeFile, g_match[g_nmatch].exe, (int)sizeof(g_match[0].exe));
                snprintf(g_match[g_nmatch].cmd, sizeof(g_match[0].cmd), "%s", cl);
                g_nmatch++;
            }
            free(cl);
        } while (Process32NextW(snap, &pe));
    }
    CloseHandle(snap);
}

/* ---------------------------------------------------------------- 局域网 IP */
static void get_lan_ip(char *out /* 至少16字节 */)
{
    out[0] = 0;
    ULONG sz = 15 * 1024;
    PIP_ADAPTER_ADDRESSES aa = (PIP_ADAPTER_ADDRESSES)malloc(sz);
    if (!aa) return;
    if (GetAdaptersAddresses(AF_INET, GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST,
                             NULL, aa, &sz) == NO_ERROR) {
        for (PIP_ADAPTER_ADDRESSES p = aa; p; p = p->Next) {
            if (p->IfType == IF_TYPE_SOFTWARE_LOOPBACK || p->OperStatus != IfOperStatusUp)
                continue;
            for (PIP_ADAPTER_UNICAST_ADDRESS ua = p->FirstUnicastAddress; ua; ua = ua->Next) {
                if (ua->Address.lpSockaddr &&
                    ua->Address.lpSockaddr->sa_family == AF_INET) {
                    unsigned char *ip = (unsigned char *)
                        &((struct sockaddr_in *)(void *)ua->Address.lpSockaddr)->sin_addr;
                    snprintf(out, 16, "%u.%u.%u.%u", ip[0], ip[1], ip[2], ip[3]);
                    free(aa);
                    return;
                }
            }
        }
    }
    free(aa);
}

/* ---------------------------------------------------------------- 打开浏览器 */
static void open_url(const wchar_t *url)
{
    static const wchar_t *cands[] = {
        L"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
        L"C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
        L"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        L"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
        L"D:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
        L"D:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
        L"D:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    };
    for (int i = 0; i < (int)(sizeof cands / sizeof cands[0]); i++) {
        if (GetFileAttributesW(cands[i]) != INVALID_FILE_ATTRIBUTES) {
            ShellExecuteW(NULL, L"open", cands[i], url, NULL, SW_SHOWNORMAL);
            return;
        }
    }
    /* 精简系统兜底：交给系统默认关联 */
    ShellExecuteW(NULL, L"open", url, NULL, NULL, SW_SHOWNORMAL);
}

/* ---------------------------------------------------------------- 启动/停止核心 */
/* 返回 0=成功；out 写结果描述（UTF-8） */
static int do_start(char *out, int outlen)
{
    int spawned = 0;
    int o = check_orthanc();
    if (o < 0) {
        WCHAR cmd[1200], wd[MAX_PATH + 8];
        _snwprintf(cmd, 1199, L"\"%ls\\orthanc\\Orthanc.exe\" \"%ls\\orthanc\\orthanc.json\"",
                   g_root, g_root);
        _snwprintf(wd, MAX_PATH + 7, L"%ls\\orthanc", g_root);
        STARTUPINFOW si; PROCESS_INFORMATION pi;
        ZeroMemory(&si, sizeof si); si.cb = sizeof si;
        /* 脱离父进程作业（若作业允许），服务不随启动器/调用方一起被回收 */
        DWORD flags = CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB;
        if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE, flags, NULL, wd, &si, &pi)) {
            flags = CREATE_NO_WINDOW;   /* 作业不允许脱离则回退 */
            if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE, flags, NULL, wd, &si, &pi)) {
                snprintf(out, (size_t)outlen, "Orthanc 启动失败（系统错误 %lu）", GetLastError());
                return 1;
            }
        }
        CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
        spawned++;
    }
    int a = check_app();
    if (a < 0) {
        WCHAR vpy[MAX_PATH * 2];
        _snwprintf(vpy, MAX_PATH * 2 - 1, L"%ls\\.venv\\Scripts\\python.exe", g_root);
        if (GetFileAttributesW(vpy) == INVALID_FILE_ATTRIBUTES) {
            snprintf(out, (size_t)outlen,
                     "未找到 .venv\\Scripts\\python.exe —— 请先安装依赖（手册 4.2 节）");
            return 1;
        }
        WCHAR cmd[1200];
        _snwprintf(cmd, 1199,
                   L"\"%ls\" -m uvicorn app.backend.main:app --host 0.0.0.0 --port 8000", vpy);
        STARTUPINFOW si; PROCESS_INFORMATION pi;
        ZeroMemory(&si, sizeof si); si.cb = sizeof si;
        DWORD flags = CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB;
        if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE, flags, NULL, g_root, &si, &pi)) {
            flags = CREATE_NO_WINDOW;
            if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE, flags, NULL, g_root, &si, &pi)) {
                snprintf(out, (size_t)outlen, "教学系统启动失败（系统错误 %lu）", GetLastError());
                return 1;
            }
        }
        CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
        spawned++;
    }
    int okO = 0, okA = 0;
    for (int i = 0; i < 100; i++) {   /* 最长 50 秒 */
        Sleep(500);
        okO = check_orthanc() == 200;
        okA = check_app() == 200;
        if (okO && okA) break;
    }
    snprintf(out, (size_t)outlen, "%s Orthanc %s · 教学系统 %s%s",
             (okO && okA) ? "服务就绪。" : "未全部就绪！",
             okO ? "正常" : "未就绪", okA ? "正常" : "未就绪",
             spawned ? "（本次已拉起）" : "（此前已在运行）");
    return (okO && okA) ? 0 : 1;
}

static int do_stop(char *out, int outlen)
{
    find_project_procs();
    int killed = 0;
    char list[1024] = "";
    for (int i = 0; i < g_nmatch; i++) {
        HANDLE h = OpenProcess(PROCESS_TERMINATE, FALSE, g_match[i].pid);
        if (h) {
            TerminateProcess(h, 1);
            CloseHandle(h);
            killed++;
            char one[160];
            snprintf(one, sizeof one, "%s(PID %lu) ", g_match[i].exe,
                     (unsigned long)g_match[i].pid);
            size_t used = strlen(list), room = sizeof list - used - 1;
            if (room > 0) strncat(list, one, room);   /* 防止 list 写满时无符号下溢 */
        }
    }
    Sleep(1200);
    int o = check_orthanc(), a = check_app();
    snprintf(out, (size_t)outlen,
             "已结束 %d 个本项目进程（%s）。Orthanc %s · 教学系统 %s",
             killed, killed ? list : "无匹配进程",
             o < 0 ? "已停止" : "仍在运行", a < 0 ? "已停止" : "仍在运行");
    return 0;
}

static int do_status(char *out, int outlen)
{
    int o = check_orthanc(), a = check_app();
    char ip[16];
    get_lan_ip(ip);
    snprintf(out, (size_t)outlen, "Orthanc=%s  教学系统=%s  局域网IP=%s",
             o == 200 ? "正常" : (o == 401 ? "认证不匹配" : (o < 0 ? "未运行" : "异常响应")),
             a == 200 ? "正常" : (a < 0 ? "未运行" : "异常响应"),
             ip[0] ? ip : "-");
    return (o == 200 && a == 200) ? 0 : 1;
}

/* ---------------------------------------------------------------- GUI 后台线程 */
static DWORD WINAPI worker(LPVOID arg)
{
    (void)arg;
    char buf[1600];
    for (;;) {
        WaitForSingleObject(g_ev, INFINITE);
        EnterCriticalSection(&g_cs);
        int job = g_job;
        g_job = -1;
        LeaveCriticalSection(&g_cs);
        if (job == JOB_START) {
            int ok = do_start(buf, (int)sizeof buf) == 0;
            int o = check_orthanc(), a = check_app();
            if (g_hwnd) {
                PostMessageW(g_hwnd, WM_APP_STATUS, (WPARAM)o, (LPARAM)a);
                PostMessageW(g_hwnd, WM_APP_DONE, (WPARAM)((ok ? 1 : 0) | (JOB_START << 16)),
                             (LPARAM)wcs_dup(u8_worker(buf)));
            }
        } else if (job == JOB_STOP) {
            int ok = do_stop(buf, (int)sizeof buf) == 0;
            int o = check_orthanc(), a = check_app();
            if (g_hwnd) {
                PostMessageW(g_hwnd, WM_APP_STATUS, (WPARAM)o, (LPARAM)a);
                PostMessageW(g_hwnd, WM_APP_DONE, (WPARAM)((ok ? 1 : 0) | (JOB_STOP << 16)),
                             (LPARAM)wcs_dup(u8_worker(buf)));
            }
        } else {                        /* JOB_REFRESH */
            int o = check_orthanc(), a = check_app();
            if (g_hwnd)
                PostMessageW(g_hwnd, WM_APP_STATUS, (WPARAM)o, (LPARAM)a);
        }
    }
    return 0;
}

static void post_job(int job)
{
    EnterCriticalSection(&g_cs);
    int pending = g_job;
    if (pending == JOB_START || pending == JOB_STOP) {
        /* 操作任务未执行完：刷新可丢弃，也不叠加第二个操作 */
        LeaveCriticalSection(&g_cs);
        return;
    }
    g_job = job;
    LeaveCriticalSection(&g_cs);
    SetEvent(g_ev);
}

/* ---------------------------------------------------------------- 赛博朋克绘制 */
static void paint_bg(HDC hdc, int w, int h)
{
    /* 双缓冲：内存 DC 绘制深色渐变 + 网格 + 角标 + 扫描线 */
    HDC mem = CreateCompatibleDC(hdc);
    HBITMAP bmp = CreateCompatibleBitmap(hdc, w, h);
    HBITMAP old = (HBITMAP)SelectObject(mem, bmp);

    /* 1) 纵向渐变背景 */
    for (int y = 0; y < h; y++) {
        double t = (double)y / (h > 0 ? h : 1);
        int r = (int)(5 + (13 - 5) * t);
        int g = (int)(9 + (22 - 9) * t);
        int b = (int)(18 + (42 - 18) * t);
        HPEN pen = CreatePen(PS_SOLID, 1, RGB(r, g, b));
        HPEN op = (HPEN)SelectObject(mem, pen);
        MoveToEx(mem, 0, y, NULL);
        LineTo(mem, w, y);
        SelectObject(mem, op);
        DeleteObject(pen);
    }

    /* 2) HUD 网格（32px） */
    HPEN grid = CreatePen(PS_SOLID, 1, GRID_COLOR);
    HPEN og = (HPEN)SelectObject(mem, grid);
    for (int x = 0; x <= w; x += 32) {
        MoveToEx(mem, x, 0, NULL);
        LineTo(mem, x, h);
    }
    for (int y = 0; y <= h; y += 32) {
        MoveToEx(mem, 0, y, NULL);
        LineTo(mem, w, y);
    }
    SelectObject(mem, og);

    /* 3) 标题分隔霓虹线：青 -> 品红渐变（y=72） */
    {
        int y = 72, x1 = 16, x2 = w - 16;
        for (int x = x1; x <= x2; x++) {
            double t = (double)(x - x1) / ((x2 - x1) > 0 ? (x2 - x1) : 1);
            HPEN pen = CreatePen(PS_SOLID, 1,
                                 RGB((int)(0 + 255 * t), (int)(240 - 195 * t), (int)(208 - 59 * t)));
            HPEN op = (HPEN)SelectObject(mem, pen);
            MoveToEx(mem, x, y, NULL);
            LineTo(mem, x, y + 2);
            SelectObject(mem, op);
            DeleteObject(pen);
        }
        /* 线两端小方块 */
        HBRUSH bc = CreateSolidBrush(NEON_CYAN), bm = CreateSolidBrush(NEON_MAG);
        RECT rc1 = { x1 - 2, y - 1, x1 + 3, y + 4 };
        RECT rc2 = { x2 - 3, y - 1, x2 + 2, y + 4 };
        FillRect(mem, &rc1, bc);
        FillRect(mem, &rc2, bm);
        DeleteObject(bc); DeleteObject(bm);
    }

    /* 4) 四角 L 形角标（8px） */
    HPEN cp = CreatePen(PS_SOLID, 1, CYAN_FAINT);
    HPEN oc = (HPEN)SelectObject(mem, cp);
    int L = 10, m = 6;
    MoveToEx(mem, m, m + L, NULL); LineTo(mem, m, m); LineTo(mem, m + L, m);
    MoveToEx(mem, w - m - L, m, NULL); LineTo(mem, w - m, m); LineTo(mem, w - m, m + L);
    MoveToEx(mem, m, h - m - L, NULL); LineTo(mem, m, h - m); LineTo(mem, m + L, h - m);
    MoveToEx(mem, w - m - L, h - m, NULL); LineTo(mem, w - m, h - m); LineTo(mem, w - m, h - m - L);
    SelectObject(mem, oc);
    DeleteObject(cp);

    /* 5) 右上角 HUD 角标文字 */
    {
        HFONT fnt = CreateFontW(-11, 0, 0, 0, FW_NORMAL, 0, 0, 0, DEFAULT_CHARSET,
                                 OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY,
                                 DEFAULT_PITCH | FF_MODERN, L"Consolas");
        HFONT of = (HFONT)SelectObject(mem, fnt);
        SetTextColor(mem, MAG_DIM);
        SetBkMode(mem, TRANSPARENT);
        TextOutW(mem, w - 132, 10, L"SYS://EDUPACS", 13);
        SelectObject(mem, of);
        DeleteObject(fnt);
    }

    /* 6) 扫描线：亮线 + 渐变尾巴（循环下移） */
    {
        int sy = g_scan_y % (h + 20);
        for (int k = 8; k >= 1; k--) {
            int yy = sy - k * 3;
            if (yy < 0 || yy >= h) continue;
            int a = 30 + (8 - k) * 12;      /* 伪透明度：越近越亮 */
            HPEN pen = CreatePen(PS_SOLID, 1, RGB(0, a, (int)(a * 0.9)));
            HPEN op = (HPEN)SelectObject(mem, pen);
            MoveToEx(mem, 0, yy, NULL);
            LineTo(mem, w, yy);
            SelectObject(mem, op);
            DeleteObject(pen);
        }
        HPEN pen = CreatePen(PS_SOLID, 2, RGB(0, 230, 198));
        HPEN op = (HPEN)SelectObject(mem, pen);
        if (sy >= 0 && sy < h) {
            MoveToEx(mem, 0, sy, NULL);
            LineTo(mem, w, sy);
        }
        SelectObject(mem, op);
        DeleteObject(pen);
    }

    /* 提交到窗口 DC */
    BitBlt(hdc, 0, 0, w, h, mem, 0, 0, SRCCOPY);
    SelectObject(mem, old);
    DeleteObject(bmp);
    DeleteDC(mem);
}

/* 按钮主题色与文字 */
static void btn_theme(int id, COLORREF *edge, COLORREF *txt, const char **label)
{
    switch (id) {
    case IDC_BTN_START: *edge = NEON_CYAN; *txt = NEON_CYAN; *label = "一键启动";   break;
    case IDC_BTN_STOP:  *edge = NEON_MAG;  *txt = NEON_MAG;  *label = "一键停止";   break;
    case IDC_BTN_APP:   *edge = CYAN_DIM; *txt = NEON_CYAN; *label = "打开教学系统"; break;
    case IDC_BTN_O:     *edge = CYAN_DIM; *txt = NEON_CYAN; *label = "打开 Orthanc 后台"; break;
    default:            *edge = TXT_FAINT; *txt = TXT_DIM;  *label = "?";          break;
    }
}

static void draw_button(DRAWITEMSTRUCT *dis)
{
    COLORREF edge, txt;
    const char *label;
    btn_theme(dis->CtlID, &edge, &txt, &label);
    int hover = (g_hover == dis->CtlID);
    int disabled = (dis->itemState & ODS_DISABLED);

    HDC hdc = dis->hDC;
    RECT rc = dis->rcItem;
    int w = rc.right - rc.left, h = rc.bottom - rc.top;

    /* 深色底（hover 提亮） */
    HBRUSH bg = CreateSolidBrush(disabled ? RGB(6, 10, 20)
                                           : (hover ? BTN_HOVER : BTN_BG));
    HBRUSH b0 = CreateSolidBrush(BG_TOP);
    FillRect(hdc, &rc, b0);          /* 外框 1px 背景色衬底 */
    RECT inner = { rc.left + 1, rc.top + 1, rc.right - 1, rc.bottom - 1 };
    FillRect(hdc, &inner, bg);
    DeleteObject(bg);
    DeleteObject(b0);

    /* 霓虹边框（hover 加粗感：双层） */
    HPEN pen = CreatePen(PS_SOLID, 1, disabled ? TXT_FAINT : (hover ? edge : RGB(
        (GetRValue(edge) * 2 + GetRValue(BTN_BG)) / 3,
        (GetGValue(edge) * 2 + GetGValue(BTN_BG)) / 3,
        (GetBValue(edge) * 2 + GetBValue(BTN_BG)) / 3)));
    HPEN op = (HPEN)SelectObject(hdc, pen);
    HBRUSH ob = (HBRUSH)SelectObject(hdc, GetStockObject(NULL_BRUSH));
    Rectangle(hdc, rc.left, rc.top, rc.right, rc.bottom);
    if (hover && !disabled) {
        HPEN pen2 = CreatePen(PS_SOLID, 1, edge);
        SelectObject(hdc, pen2);
        Rectangle(hdc, rc.left + 2, rc.top + 2, rc.right - 2, rc.bottom - 2);
        SelectObject(hdc, pen);
        DeleteObject(pen2);
    }
    SelectObject(hdc, ob);
    SelectObject(hdc, op);
    DeleteObject(pen);

    /* 左上/右下小三角点缀（赛博切角感） */
    {
        HPEN tp = CreatePen(PS_SOLID, 1, disabled ? TXT_FAINT : edge);
        HPEN otp = (HPEN)SelectObject(hdc, tp);
        MoveToEx(hdc, rc.left + 1, rc.top + 5, NULL);
        LineTo(hdc, rc.left + 6, rc.top + 1);
        MoveToEx(hdc, rc.right - 1, rc.bottom - 5, NULL);
        LineTo(hdc, rc.right - 6, rc.bottom - 1);
        SelectObject(hdc, otp);
        DeleteObject(tp);
    }

    /* 文字 */
    SetBkMode(hdc, TRANSPARENT);
    SetTextColor(hdc, disabled ? TXT_FAINT : txt);
    wchar_t *ws = u8(label);
    int oldmode = SetTextAlign(hdc, TA_CENTER | TA_BASELINE);
    TextOutW(hdc, rc.left + w / 2, rc.top + h / 2 + 5, ws, (int)wcslen(ws));
    SetTextAlign(hdc, oldmode);

    if (dis->itemState & ODS_FOCUS) { /* 无边框焦点提示：底部小短线 */
        HPEN fp = CreatePen(PS_SOLID, 1, edge);
        HPEN ofp = (HPEN)SelectObject(hdc, fp);
        MoveToEx(hdc, rc.left + w / 3, rc.bottom - 4, NULL);
        LineTo(hdc, rc.left + w * 2 / 3, rc.bottom - 4);
        SelectObject(hdc, ofp);
        DeleteObject(fp);
    }
}

/* ---------------------------------------------------------------- 窗口过程 */
static void update_status_text(HWND hwnd, int code, int id, int *st, const char *name)
{
    *st = (code == 200) ? 1 : (code == 401 ? 2 : 0);
    char s[128];
    if (*st == 1)
        snprintf(s, sizeof s, "▸ %s   ONLINE  ·  运行正常", name);
    else if (*st == 2)
        snprintf(s, sizeof s, "▸ %s   AUTH-ERR  ·  认证不匹配（检查 orthanc.json）", name);
    else
        snprintf(s, sizeof s, "▸ %s   OFFLINE  ·  未运行", name);
    SetWindowTextW(GetDlgItem(hwnd, id), u8(s));
}

static LRESULT CALLBACK wndproc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    static HFONT hf = NULL, hfb = NULL, hfm = NULL;
    static TRACKMOUSEEVENT tme;
    switch (msg) {
    case WM_CREATE: {
        hf  = CreateFontW(-14, 0, 0, 0, FW_NORMAL, 0, 0, 0, DEFAULT_CHARSET,
                          OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY,
                          DEFAULT_PITCH | FF_DONTCARE, L"Microsoft YaHei UI");
        hfb = CreateFontW(-21, 0, 0, 0, FW_SEMIBOLD, 0, 0, 0, DEFAULT_CHARSET,
                          OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY,
                          DEFAULT_PITCH | FF_DONTCARE, L"Microsoft YaHei UI");
        hfm = CreateFontW(-13, 0, 0, 0, FW_NORMAL, 0, 0, 0, DEFAULT_CHARSET,
                          OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY,
                          DEFAULT_PITCH | FF_MODERN, L"Consolas");

        HWND c;
        c = CreateWindowW(L"STATIC", u8("EduPACS 教学病例库启动器"),
                          WS_CHILD | WS_VISIBLE, 16, 12, 430, 34,
                          hwnd, (HMENU)(INT_PTR)IDC_TITLE, NULL, NULL);
        SendMessageW(c, WM_SETFONT, (WPARAM)hfb, TRUE);
        c = CreateWindowW(L"STATIC", L"// TEACHING PACS · CONTROL PANEL",
                          WS_CHILD | WS_VISIBLE, 17, 48, 430, 18,
                          hwnd, (HMENU)(INT_PTR)0, NULL, NULL);
        SendMessageW(c, WM_SETFONT, (WPARAM)hfm, TRUE);
        c = CreateWindowW(L"STATIC", u8("▸ Orthanc   SCANNING…"),
                          WS_CHILD | WS_VISIBLE, 16, 86, 450, 22,
                          hwnd, (HMENU)(INT_PTR)IDC_ST_O, NULL, NULL);
        SendMessageW(c, WM_SETFONT, (WPARAM)hf, TRUE);
        c = CreateWindowW(L"STATIC", u8("▸ 教学系统  SCANNING…"),
                          WS_CHILD | WS_VISIBLE, 16, 112, 450, 22,
                          hwnd, (HMENU)(INT_PTR)IDC_ST_A, NULL, NULL);
        SendMessageW(c, WM_SETFONT, (WPARAM)hf, TRUE);

        /* 按钮（owner-draw 赛博风） */
        struct { int id; const char *txt; int x, y, w, h; } btn[] = {
            { IDC_BTN_START, "一键启动",          16,  146, 128, 40 },
            { IDC_BTN_STOP,  "一键停止",          152, 146, 128, 40 },
            { IDC_BTN_APP,   "打开教学系统",      288, 146, 150, 40 },
            { IDC_BTN_O,     "打开 Orthanc 后台", 16,  194, 200, 36 },
        };
        for (int i = 0; i < 4; i++) {
            HWND b = CreateWindowW(L"BUTTON", u8(btn[i].txt),
                                   WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_OWNERDRAW,
                                   btn[i].x, btn[i].y, btn[i].w, btn[i].h,
                                   hwnd, (HMENU)(INT_PTR)btn[i].id, NULL, NULL);
            (void)b;
        }

        c = CreateWindowW(L"STATIC", L"",
                          WS_CHILD | WS_VISIBLE, 16, 246, 430, 44,
                          hwnd, (HMENU)(INT_PTR)IDC_ADDR, NULL, NULL);
        SendMessageW(c, WM_SETFONT, (WPARAM)hf, TRUE);
        c = CreateWindowW(L"STATIC", L"",
                          WS_CHILD | WS_VISIBLE, 16, 296, 430, 56,
                          hwnd, (HMENU)(INT_PTR)IDC_TIP, NULL, NULL);
        SendMessageW(c, WM_SETFONT, (WPARAM)hf, TRUE);
        c = CreateWindowW(L"STATIC", u8("> 就绪"),
                          WS_CHILD | WS_VISIBLE, 16, 354, 430, 22,
                          hwnd, (HMENU)(INT_PTR)IDC_STATUS, NULL, NULL);
        SendMessageW(c, WM_SETFONT, (WPARAM)hfm, TRUE);

        {
            char addr[256], tip[512], ip[16];
            get_lan_ip(ip);
            snprintf(addr, sizeof addr,
                     "本机访问：http://127.0.0.1:8000\r\n"
                     "局域网访问：http://%s:8000（师生电脑）",
                     ip[0] ? ip : "<本机IP>");
            snprintf(tip, sizeof tip,
                     "默认账号 admin / admin123（首次登录强制改密）\r\n"
                     "Orthanc 后台 8042 已认证（账号见 orthanc.json）\r\n"
                     "一键停止只结束本项目进程，不影响同机其它服务");
            SetWindowTextW(GetDlgItem(hwnd, IDC_ADDR), u8(addr));
            SetWindowTextW(GetDlgItem(hwnd, IDC_TIP), u8(tip));
        }
        SetTimer(hwnd, TIMER_REFRESH, 2000, NULL);
        SetTimer(hwnd, TIMER_SCAN, 40, NULL);
        tme.cbSize = sizeof tme; tme.dwFlags = TME_LEAVE; tme.hwndTrack = hwnd;
        post_job(JOB_REFRESH);           /* 立即做一次状态检查 */
        return 0;
    }
    case WM_ERASEBKGND:
        return 1;                          /* 擦除交给 WM_PAINT 双缓冲 */
    case WM_PAINT: {
        PAINTSTRUCT ps;
        HDC hdc = BeginPaint(hwnd, &ps);
        RECT rc;
        GetClientRect(hwnd, &rc);
        paint_bg(hdc, rc.right, rc.bottom);
        EndPaint(hwnd, &ps);
        return 0;
    }
    case WM_PRINTCLIENT:                   /* 打印/截图支持 */
        paint_bg((HDC)wp, 500, 380);
        return 0;
    case WM_TIMER:
        if (wp == TIMER_SCAN) {
            g_scan_y = (g_scan_y + 2) % 4000;
            HDC hdc = GetDC(hwnd);
            RECT rc;
            GetClientRect(hwnd, &rc);
            paint_bg(hdc, rc.right, rc.bottom);
            ReleaseDC(hwnd, hdc);
        } else if (wp == TIMER_REFRESH) {
            post_job(JOB_REFRESH);
        }
        return 0;
    case WM_MOUSEMOVE: {
        /* hover 检测（按钮亮起） */
        int ids[] = { IDC_BTN_START, IDC_BTN_STOP, IDC_BTN_APP, IDC_BTN_O };
        POINT pt = { LOWORD(lp), HIWORD(lp) };
        int hit = 0;
        for (int i = 0; i < 4 && !hit; i++) {
            HWND b = GetDlgItem(hwnd, ids[i]);
            RECT r;
            GetWindowRect(b, &r);
            POINT scr = pt;
            ClientToScreen(hwnd, &scr);
            if (scr.x >= r.left && scr.x <= r.right && scr.y >= r.top && scr.y <= r.bottom)
                hit = ids[i];
        }
        if (hit != g_hover) {
            int oldh = g_hover;
            g_hover = hit;
            if (oldh) InvalidateRect(GetDlgItem(hwnd, oldh), NULL, TRUE);
            if (hit)  InvalidateRect(GetDlgItem(hwnd, hit), NULL, TRUE);
        }
        static int tracked = 0;
        if (!tracked) {
            TrackMouseEvent(&tme);
            tracked = 1;
        }
        return 0;
    }
    case WM_MOUSELEAVE: {
        if (g_hover) {
            int oldh = g_hover;
            g_hover = 0;
            InvalidateRect(GetDlgItem(hwnd, oldh), NULL, TRUE);
        }
        /* 重新武装下一次离开检测 */
        KillTimer(hwnd, TIMER_TRAK);
        tme.cbSize = sizeof tme; tme.dwFlags = TME_LEAVE; tme.hwndTrack = hwnd;
        SetTimer(hwnd, TIMER_TRAK, 50, NULL);
        return 0;
    }
    case WM_APP_STATUS:
        update_status_text(hwnd, (int)wp, IDC_ST_O, &g_st_o, "Orthanc");
        update_status_text(hwnd, (int)lp, IDC_ST_A, &g_st_a, "教学系统");
        return 0;
    case WM_APP_DONE: {
        int job = (int)(wp >> 16) & 0xffff;
        int ok = (int)wp & 1;
        wchar_t *text = (wchar_t *)lp;
        g_busy = 0;
        EnableWindow(GetDlgItem(hwnd, IDC_BTN_START), TRUE);
        EnableWindow(GetDlgItem(hwnd, IDC_BTN_STOP), TRUE);
        if (text) {
            /* 状态行加 "> " 控制台前缀 */
            wchar_t buf2[1200];
            _snwprintf(buf2, 1199, L"> %ls", text);
            SetWindowTextW(GetDlgItem(hwnd, IDC_STATUS), buf2);
            free(text);
        }
        if (ok && job == JOB_START)
            open_url(L"http://127.0.0.1:8000");
        return 0;
    }
    case WM_COMMAND: {
        int id = LOWORD(wp);
        if (id == IDC_BTN_START && !g_busy) {
            g_busy = 1;
            EnableWindow(GetDlgItem(hwnd, IDC_BTN_START), FALSE);
            EnableWindow(GetDlgItem(hwnd, IDC_BTN_STOP), FALSE);
            SetWindowTextW(GetDlgItem(hwnd, IDC_STATUS),
                           u8("> 正在启动服务…（最长等待 50 秒）"));
            post_job(JOB_START);
        } else if (id == IDC_BTN_STOP && !g_busy) {
            g_busy = 1;
            EnableWindow(GetDlgItem(hwnd, IDC_BTN_START), FALSE);
            EnableWindow(GetDlgItem(hwnd, IDC_BTN_STOP), FALSE);
            SetWindowTextW(GetDlgItem(hwnd, IDC_STATUS), u8("> 正在安全停止服务…"));
            post_job(JOB_STOP);
        } else if (id == IDC_BTN_APP) {
            open_url(L"http://127.0.0.1:8000");
        } else if (id == IDC_BTN_O) {
            open_url(L"http://127.0.0.1:8042/");
        }
        return 0;
    }
    case WM_DRAWITEM:
        draw_button((DRAWITEMSTRUCT *)lp);
        return TRUE;
    case WM_CTLCOLORSTATIC: {
        HDC hdc = (HDC)wp;
        int id = GetDlgCtrlID((HWND)lp);
        COLORREF c;
        HBRUSH br = CreateSolidBrush(BG_TOP);   /* 深色实心背景（不叠印） */
        if (id == IDC_TITLE)
            c = NEON_CYAN;
        else if (id == IDC_ST_O)
            c = g_st_o == 1 ? NEON_CYAN : (g_st_o == 2 ? AMBER_WARN : RED_OFF);
        else if (id == IDC_ST_A)
            c = g_st_a == 1 ? NEON_CYAN : (g_st_a == 2 ? AMBER_WARN : RED_OFF);
        else if (id == IDC_STATUS)
            c = CYAN_DIM;
        else if (id == IDC_ADDR)
            c = TXT_DIM;
        else if (id == IDC_TIP)
            c = TXT_FAINT;
        else
            c = MAG_DIM;                        /* 副标题 */
        SetTextColor(hdc, c);
        SetBkColor(hdc, BG_TOP);
        return (LRESULT)br;                     /* 由系统 DeleteObject 临时画刷 */
    }
    case WM_CLOSE:
        DestroyWindow(hwnd);
        return 0;
    case WM_DESTROY:
        KillTimer(hwnd, TIMER_REFRESH);
        KillTimer(hwnd, TIMER_SCAN);
        KillTimer(hwnd, TIMER_TRAK);
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

static int run_gui(void)
{
    WNDCLASSEXW wc;
    ZeroMemory(&wc, sizeof wc);
    wc.cbSize = sizeof wc;
    wc.lpfnWndProc = wndproc;
    HINSTANCE hInst = GetModuleHandleW(NULL);
    wc.hInstance = hInst;
    wc.hCursor = LoadCursorW(NULL, (LPCWSTR)IDC_ARROW);
    /* 优先用嵌入的自有图标，缺失时退回系统默认 */
    wc.hIcon = (HICON)LoadImageW(hInst, MAKEINTRESOURCEW(IDI_APPICON), IMAGE_ICON, 0, 0,
                                 LR_DEFAULTSIZE | LR_SHARED);
    wc.hIconSm = (HICON)LoadImageW(hInst, MAKEINTRESOURCEW(IDI_APPICON), IMAGE_ICON, 16, 16,
                                   LR_SHARED);
    if (!wc.hIcon) wc.hIcon = LoadIconW(NULL, (LPCWSTR)IDI_APPLICATION);
    wc.hbrBackground = NULL;                 /* WM_PAINT 双缓冲自绘 */
    wc.lpszClassName = L"EduPacsLauncherWnd";
    RegisterClassExW(&wc);

    RECT r = { 0, 0, 500, 382 };
    AdjustWindowRect(&r, WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX, FALSE);

    g_hwnd = CreateWindowW(wc.lpszClassName, u8("EduPACS 教学病例库启动器"),
                           WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX
                           | WS_CLIPCHILDREN,   /* 背景重绘不覆盖子控件（扫描线动画关键） */
                           CW_USEDEFAULT, CW_USEDEFAULT,
                           r.right - r.left, r.bottom - r.top,
                           NULL, NULL, wc.hInstance, NULL);
    if (!g_hwnd) return 1;
    ShowWindow(g_hwnd, SW_SHOW);
    UpdateWindow(g_hwnd);

    MSG m;
    while (GetMessageW(&m, NULL, 0, 0) > 0) {
        TranslateMessage(&m);
        DispatchMessageW(&m);
    }
    return 0;
}

/* ---------------------------------------------------------------- CLI 模式 */
static void attach_console(void)
{
    AttachConsole(ATTACH_PARENT_PROCESS);
    if (GetConsoleWindow()) {
        freopen("CONOUT$", "w", stdout);
        freopen("CONOUT$", "w", stderr);
        SetConsoleOutputCP(65001);
    }
}

static int run_cli(wchar_t *argw)
{
    attach_console();
    char arg[64];
    w2u8(argw, arg, (int)sizeof arg);

    char buf[1600];
    int rc = 0;
    if (!strcmp(arg, "--help") || !strcmp(arg, "-h") || !strcmp(arg, "/?")) {
        printf("EduPACS 教学病例库启动器（原生版）\n"
               "用法：\n"
               "  EduPACS启动器.exe            打开图形界面\n"
               "  EduPACS启动器.exe --status   查看服务状态（退出码 0=两服务就绪，1=有服务未就绪）\n"
               "  EduPACS启动器.exe --start    一键启动并等待就绪（退出码 0=成功）\n"
               "  EduPACS启动器.exe --stop     安全停止（只结束本项目进程）\n");
        rc = 0;
    } else if (!strcmp(arg, "--status")) {
        rc = do_status(buf, (int)sizeof buf);
        printf("%s\n", buf);
    } else if (!strcmp(arg, "--start")) {
        rc = do_start(buf, (int)sizeof buf);
        printf("%s\n", buf);
    } else if (!strcmp(arg, "--stop")) {
        rc = do_stop(buf, (int)sizeof buf);
        printf("%s\n", buf);
    } else {
        printf("未知参数：%s（支持 --status / --start / --stop / --help）\n", arg);
        rc = 1;
    }
    /* GUI 子系统进程 attach 了父控制台：退出前释放，避免等待方挂在控制台引用上 */
    fflush(stdout);
    FreeConsole();
    return rc;
}

int main(void)
{
    SetProcessDPIAware();
    load_config();

    int argc = 0;
    wchar_t **argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (argv && argc > 1) {
        int rc = run_cli(argv[1]);
        LocalFree(argv);
        return rc;
    }

    InitializeCriticalSection(&g_cs);
    g_ev = CreateEventW(NULL, FALSE /*自动复位*/, FALSE, NULL);
    if (!g_ev) return 1;
    HANDLE ht = CreateThread(NULL, 0, worker, NULL, 0, NULL);
    if (ht) CloseHandle(ht);

    int rc = run_gui();
    CloseHandle(g_ev);
    DeleteCriticalSection(&g_cs);
    return rc;
}

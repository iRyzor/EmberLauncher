/* Public runtime availability only; no SDK initialization or account access. */
#include <windows.h>
#include <objbase.h>
#include <stdio.h>
#include <wchar.h>
typedef HRESULT (STDAPICALLTYPE *WebViewVersionFn)(PCWSTR, LPWSTR *);
int wmain(int argc, wchar_t **argv) {
    if (argc != 2) return 2;
    HMODULE module = LoadLibraryExW(argv[1], NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!module) { printf("LOADER_ERROR=%lu\n", GetLastError()); return 3; }
    WebViewVersionFn query = (WebViewVersionFn)(void *)GetProcAddress(module, "GetAvailableCoreWebView2BrowserVersionString");
    if (!query) { FreeLibrary(module); return 4; }
    LPWSTR version = NULL;
    HRESULT result = query(NULL, &version);
    if (SUCCEEDED(result) && version && wcslen(version) < 128) {
        printf("WEBVIEW2_VERSION=%ls\n", version);
    } else printf("WEBVIEW2_ERROR=%08lx\n", (unsigned long)result);
    CoTaskMemFree(version); FreeLibrary(module);
    return SUCCEEDED(result) ? 0 : 5;
}

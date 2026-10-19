/* Replay observed API failures without stopping services or reading device identities. */
#include <winsock2.h>
#include <windows.h>
#include <iphlpapi.h>
#include <assert.h>
#include <stdio.h>
#include <string.h>

static BOOL emulate_wine = TRUE, ready;
static DWORD network_error, start_error;
static int starts, probes, pipes, locked_attempts;

static DWORD WINAPI test_adapters(PIP_ADAPTER_INFO info, PULONG size) {
    assert(info == NULL);
    probes++;
    *size = ready ? sizeof(IP_ADAPTER_INFO) : 0;
    return ready ? ERROR_BUFFER_OVERFLOW : network_error;
}
static FARPROC WINAPI test_getproc(HMODULE module, LPCSTR name) {
    return emulate_wine ? GetProcAddress(module, name) : NULL;
}
static BOOL WINAPI test_start(SC_HANDLE service, DWORD count, LPCWSTR *args) {
    assert(service && count == 0 && args == NULL);
    starts++;
    if (locked_attempts > 0) {
        locked_attempts--;
        SetLastError(ERROR_SERVICE_DATABASE_LOCKED);
        return FALSE;
    }
    if (start_error == ERROR_SUCCESS || start_error == ERROR_SERVICE_ALREADY_RUNNING) ready = TRUE;
    SetLastError(start_error);
    return start_error == ERROR_SUCCESS;
}
static HANDLE test_pipe(void) {
    pipes++;
    assert(!emulate_wine || ready);
    SetLastError(ERROR_ACCESS_DENIED);
    return INVALID_HANDLE_VALUE;
}

#define GetAdaptersInfo test_adapters
#define GetProcAddress test_getproc
#define StartServiceW test_start
#define CreateNamedPipeW(...) test_pipe()
#define wmain bridge_wmain
#include "../../engine/native/pipe_bridge.c"
#undef wmain

static void reset(void) {
    emulate_wine = TRUE;
    ready = FALSE;
    network_error = ERROR_PATH_NOT_FOUND;
    start_error = ERROR_SUCCESS;
    starts = probes = pipes = locked_attempts = 0;
}
int main(void) {
    reset();
    assert(prepare_network() == ERROR_SUCCESS && ready && starts == 1);
    assert(prepare_network() == ERROR_SUCCESS && starts == 1);
    reset();
    network_error = ERROR_FILE_NOT_FOUND;
    assert(prepare_network() == ERROR_SUCCESS && ready && starts == 1);
    reset();
    start_error = ERROR_SERVICE_ALREADY_RUNNING;
    assert(prepare_network() == ERROR_SUCCESS && ready && starts == 1);
    reset();
    locked_attempts = 3;
    assert(prepare_network() == ERROR_SUCCESS && ready && starts == 4);
    reset();
    network_error = ERROR_ACCESS_DENIED;
    assert(prepare_network() == ERROR_ACCESS_DENIED && starts == 0);
    reset();
    emulate_wine = FALSE;
    assert(prepare_network() == ERROR_PATH_NOT_FOUND && starts == 0);
    wchar_t *args[] = {L"bridge", L"\\\\.\\pipe\\Ember-policy-test", L"1"};
    reset();
    start_error = ERROR_SERVICE_DISABLED;
    assert(bridge_wmain(3, args) == 6 && starts == 1 && pipes == 0);
    reset();
    assert(bridge_wmain(3, args) == 3 && starts == 1 && ready && pipes == 1);
    reset();
    emulate_wine = FALSE;
    assert(bridge_wmain(3, args) == 3 && starts == 0 && probes == 0 && pipes == 1);
    puts("NETWORK_POLICY_TESTS_OK");
    return 0;
}

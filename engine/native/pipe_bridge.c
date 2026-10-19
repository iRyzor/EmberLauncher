/* Duplex Win32 byte pipe <-> inherited binary stdin/stdout. No account logs. */
#include <winsock2.h>
#include <windows.h>
#include <iphlpapi.h>
#include <stdio.h>
#include <stdint.h>
#include <wchar.h>
#include <fcntl.h>
#include <io.h>

/* Recover only Wine's network inventory driver. Never collect adapter addresses. */
static BOOL under_wine(void) {
    return GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "wine_get_version") != NULL;
}

static DWORD network_status(void) {
    ULONG size = 0;
    DWORD result = GetAdaptersInfo(NULL, &size);
    if ((result == ERROR_BUFFER_OVERFLOW || result == ERROR_SUCCESS) && size > 0) return ERROR_SUCCESS;
    return result == ERROR_SUCCESS ? ERROR_NO_DATA : result;
}

static DWORD prepare_network(void) {
    DWORD result = network_status();
    if (result == ERROR_SUCCESS || !under_wine() ||
        (result != ERROR_FILE_NOT_FOUND && result != ERROR_PATH_NOT_FOUND)) return result;
    SC_HANDLE manager = OpenSCManagerW(NULL, NULL, SC_MANAGER_CONNECT);
    if (!manager) return GetLastError();
    SC_HANDLE service = OpenServiceW(manager, L"nsiproxy", SERVICE_START | SERVICE_QUERY_STATUS);
    if (!service) { result = GetLastError(); CloseServiceHandle(manager); return result; }
    ULONGLONG deadline = GetTickCount64() + 8000;
    do {
        BOOL started = StartServiceW(service, 0, NULL);
        result = started ? ERROR_SUCCESS : GetLastError();
        if (result != ERROR_SERVICE_DATABASE_LOCKED &&
            result != ERROR_SERVICE_CANNOT_ACCEPT_CTRL &&
            result != ERROR_SERVICE_REQUEST_TIMEOUT) break;
        if (GetTickCount64() >= deadline) break;
        Sleep(100);
    } while (GetTickCount64() < deadline);
    if (result == ERROR_SUCCESS || result == ERROR_SERVICE_ALREADY_RUNNING) {
        do {
            result = network_status();
            if (result == ERROR_SUCCESS) break;
            SERVICE_STATUS_PROCESS status;
            DWORD bytes = 0;
            if (!QueryServiceStatusEx(service, SC_STATUS_PROCESS_INFO, (BYTE *)&status,
                                      sizeof(status), &bytes)) { result = GetLastError(); break; }
            if (status.dwCurrentState == SERVICE_STOPPED) {
                result = status.dwWin32ExitCode ? status.dwWin32ExitCode : ERROR_SERVICE_NOT_ACTIVE;
                break;
            }
            Sleep(100);
        } while (GetTickCount64() < deadline);
    }
    CloseServiceHandle(service);
    CloseServiceHandle(manager);
    return result;
}

typedef struct { HANDLE pipe; HANDLE stop; DWORD timeout; } Bridge;

static BOOL transfer(HANDLE pipe, unsigned char *buffer, DWORD count,
                     BOOL writing, DWORD timeout) {
    DWORD offset = 0;
    ULONGLONG deadline = GetTickCount64() + timeout;
    while (offset < count) {
        OVERLAPPED ov = {0};
        DWORD done = 0;
        ov.hEvent = CreateEventW(NULL, TRUE, FALSE, NULL);
        if (!ov.hEvent) return FALSE;
        BOOL ok = writing ? WriteFile(pipe, buffer + offset, count - offset, &done, &ov)
                          : ReadFile(pipe, buffer + offset, count - offset, &done, &ov);
        if (!ok && GetLastError() == ERROR_IO_PENDING) {
            ULONGLONG now = GetTickCount64();
            DWORD left = now >= deadline ? 0 : (DWORD)(deadline - now);
            if (WaitForSingleObject(ov.hEvent, left) != WAIT_OBJECT_0) {
                CancelIoEx(pipe, &ov);
                WaitForSingleObject(ov.hEvent, INFINITE);
                GetOverlappedResult(pipe, &ov, &done, FALSE);
                ok = FALSE;
            } else ok = GetOverlappedResult(pipe, &ov, &done, FALSE);
        }
        CloseHandle(ov.hEvent);
        if (!ok || !done) return FALSE;
        offset += done;
    }
    return TRUE;
}

static BOOL stream_exact(HANDLE stream, unsigned char *buffer, DWORD count, BOOL writing) {
    DWORD offset = 0;
    while (offset < count) {
        DWORD done = 0;
        BOOL ok = writing ? WriteFile(stream, buffer + offset, count - offset, &done, NULL)
                          : ReadFile(stream, buffer + offset, count - offset, &done, NULL);
        if (!ok || !done) return FALSE;
        offset += done;
    }
    return TRUE;
}

static DWORD WINAPI input_thread(void *context) {
    Bridge *bridge = context;
    unsigned char buffer[65535];
    HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
    while (WaitForSingleObject(bridge->stop, 0) == WAIT_TIMEOUT) {
        if (!stream_exact(input, buffer, 4, FALSE)) break;
        DWORD size = (DWORD)buffer[0] | ((DWORD)buffer[1] << 8);
        if (size < 4 || !stream_exact(input, buffer + 4, size - 4, FALSE) ||
            !transfer(bridge->pipe, buffer, size, TRUE, bridge->timeout)) break;
    }
    SetEvent(bridge->stop);
    CancelIoEx(bridge->pipe, NULL);
    return 0;
}

int wmain(int argc, wchar_t **argv) {
    if (argc == 2 && (!wcscmp(argv[1], L"--check-network") ||
                      !wcscmp(argv[1], L"--prepare-network"))) {
        DWORD result = !wcscmp(argv[1], L"--prepare-network") ? prepare_network() : network_status();
        if (result == ERROR_SUCCESS) puts("NETWORK_READY");
        else printf("NETWORK_ERROR=%lu\n", result);
        return result == ERROR_SUCCESS ? 0 : 6;
    }
    wchar_t *end = NULL;
    long seconds = argc == 3 ? wcstol(argv[2], &end, 10) : 0;
    if (argc != 3 || !end || *end || seconds < 1 || seconds > 300 ||
        wcsncmp(argv[1], L"\\\\.\\pipe\\", 9) || wcslen(argv[1]) > 240) return 2;
    if (under_wine()) {
        DWORD result = prepare_network();
        if (result != ERROR_SUCCESS) {
            fprintf(stderr, "NETWORK_ERROR=%lu\n", result);
            return 6;
        }
    }
    HANDLE pipe = CreateNamedPipeW(argv[1], PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED |
        FILE_FLAG_FIRST_PIPE_INSTANCE, PIPE_TYPE_BYTE | PIPE_READMODE_BYTE |
        PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS, 1, 65535, 65535, 0, NULL);
    if (pipe == INVALID_HANDLE_VALUE) {
        fputs("Cannot create exclusive local pipe.\n", stderr); return 3;
    }
    fputs("PIPE_READY\n", stderr); fflush(stderr);
    OVERLAPPED ov = {0};
    ov.hEvent = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (!ov.hEvent) { CloseHandle(pipe); return 3; }
    BOOL ok = ConnectNamedPipe(pipe, &ov);
    DWORD error = ok ? ERROR_SUCCESS : GetLastError(), done = 0;
    if (!ok && error == ERROR_IO_PENDING) {
        if (WaitForSingleObject(ov.hEvent, (DWORD)seconds * 1000) == WAIT_OBJECT_0)
            ok = GetOverlappedResult(pipe, &ov, &done, FALSE);
        else {
            CancelIoEx(pipe, &ov); WaitForSingleObject(ov.hEvent, INFINITE);
            GetOverlappedResult(pipe, &ov, &done, FALSE);
        }
    } else if (error == ERROR_PIPE_CONNECTED) ok = TRUE;
    CloseHandle(ov.hEvent);
    if (!ok) { CloseHandle(pipe); fputs("Pipe connection failed.\n", stderr); return 4; }
    Bridge bridge = {pipe, CreateEventW(NULL, TRUE, FALSE, NULL), (DWORD)seconds * 1000};
    if (!bridge.stop) { CloseHandle(pipe); return 3; }
    HANDLE thread = CreateThread(NULL, 0, input_thread, &bridge, 0, NULL);
    if (!thread) { CloseHandle(bridge.stop); CloseHandle(pipe); return 3; }
    unsigned char buffer[65535];
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    while (WaitForSingleObject(bridge.stop, 0) == WAIT_TIMEOUT) {
        if (!transfer(pipe, buffer, 4, FALSE, bridge.timeout)) break;
        DWORD size = (DWORD)buffer[0] | ((DWORD)buffer[1] << 8);
        if (size < 4 || !transfer(pipe, buffer + 4, size - 4, FALSE, bridge.timeout) ||
            !stream_exact(output, buffer, size, TRUE)) break;
    }
    SetEvent(bridge.stop);
    CancelIoEx(pipe, NULL);
    CancelSynchronousIo(thread);
    /* ExitProcess releases a stdin reader that cannot be cancelled on this Wine
     * version. Do not release its stack context before it has stopped. */
    if (WaitForSingleObject(thread, 2000) != WAIT_OBJECT_0) ExitProcess(0);
    CloseHandle(thread); CloseHandle(bridge.stop);
    DisconnectNamedPipe(pipe); CloseHandle(pipe);
    return 0;
}

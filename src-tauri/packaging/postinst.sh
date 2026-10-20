#!/bin/sh
# Notify when 32-bit libraries needed by Proton and Wine builds are missing.
set -e

if [ "$1" = "configure" ]; then
    missing=""
    if ! dpkg --print-foreign-architectures 2>/dev/null | grep -qx i386; then
        missing="the i386 architecture is not enabled"
    elif ! dpkg-query -W -f='${Status}' libfreetype6:i386 2>/dev/null | grep -q "install ok installed"; then
        missing="32-bit FreeType is not installed"
    fi
    if [ -n "$missing" ]; then
        cat >&2 <<MSG

Ember Launcher: $missing.
Proton and some Wine builds include 32-bit components. Without 32-bit libraries
the WebView2 installer fails and the game can show wrong window sizes or crash.
This is not needed if your Wine package already provides them. To install them:

    sudo dpkg --add-architecture i386
    sudo apt update
    sudo apt install libfreetype6:i386 libgnutls30t64:i386   # libgnutls30:i386 before Ubuntu 24.04 / Debian 13

MSG
    fi
fi

exit 0

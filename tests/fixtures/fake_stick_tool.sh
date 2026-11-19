#!/bin/sh
# A SIMULATED Stick tool for the deploy-script test: records what would be sent to a device and answers hash queries from what it "received".
# Arguments seen: connect PORT resume <command...>
shift 3
echo "$@" >> "$FAKE_TOOL_LOG"
case "$1" in
  fs)
    shift
    [ "$1" = "--no-verbose" ] && shift
    case "$1" in
      cp) cp "$2" "$FAKE_TOOL_STORE/$(basename "${3#:/flash/}")" ;;
      sha256sum) shasum -a 256 "$FAKE_TOOL_STORE/$(basename "${2#:/flash/}")" | awk '{print $1 "  x"}' ;;
    esac ;;
esac
exit 0

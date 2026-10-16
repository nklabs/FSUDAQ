#!/bin/bash
# Check and install everything FSUDAQ needs on Ubuntu/Debian, then build FSUDAQ_Qt6.
#
#   ./install.sh --check        report only: no sudo, nothing changed; exit 1 if anything is missing
#   ./install.sh                install what is missing, build, report
#
# Options:
#   --caen-dir DIR   where the CAEN downloads (*.tgz) are; default ~/caen
#   --no-driver      skip the CAEN USB driver (boards on optical link / A4818 only)
#   --no-build       skip building FSUDAQ_Qt6
#   --no-venv        skip the Python venv for the online dashboard (online/venv)
#
# The CAEN packages cannot be fetched automatically (caen.it needs a login). Download these
# into the CAEN directory first, then run this script; it installs them in the right order:
#   CAENVMELib-*.tgz     https://www.caen.it/products/caenvmelib-library/
#   CAENComm-*.tgz       https://www.caen.it/products/caencomm-library/
#   CAENDigitizer-*.tgz  https://www.caen.it/products/caendigitizer-library/
#   CAENUSBdrvB-*.tgz    (USB boards) not on the digitizer pages: it is the "Linux driver" under
#                        Software/Firmware on https://www.caen.it/products/v1718/ (desktop
#                        digitizers appear on USB as V1718s). 1.5.4 builds on new kernels with
#                        the patch this script applies.
#
# Safe to run again: steps that are already done are skipped.

cd "$(dirname "$(readlink -f "$0")")" || exit 1
FSUDAQ_DIR=$(pwd)

CHECK_ONLY=0
CAEN_DIR="$HOME/caen"
DO_DRIVER=1
DO_BUILD=1
DO_VENV=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)     CHECK_ONLY=1 ;;
    --caen-dir)  CAEN_DIR="${2:?--caen-dir needs a directory}"; shift ;;
    --no-driver) DO_DRIVER=0 ;;
    --no-build)  DO_BUILD=0 ;;
    --no-venv)   DO_VENV=0 ;;
    -h|--help)   sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (see --help)"; exit 2 ;;
  esac
  shift
done
CAEN_DIR=$(readlink -f "$CAEN_DIR")

caen_url() {
  case "$1" in
    CAENVMELib)    echo "https://www.caen.it/products/caenvmelib-library/" ;;
    CAENComm)      echo "https://www.caen.it/products/caencomm-library/" ;;
    CAENDigitizer) echo "https://www.caen.it/products/caendigitizer-library/" ;;
    CAENUSBdrvB)   echo "https://www.caen.it/products/v1718/ (Software/Firmware, Linux driver)" ;;
  esac
}

# Minimum versions from README.md
MIN_CAENVME=3.3
MIN_CAENComm=1.5.3
MIN_CAENDigitizer=2.17.1
MIN_USBDRV=1.5.5

#----------------------------------------------------------------------------- output helpers
if [[ -t 1 ]]; then G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; B=$'\e[1m'; N=$'\e[0m'; else G=; Y=; R=; B=; N=; fi
SUMMARY=()
NFAIL=0
section() { echo; echo "${B}== $*${N}"; }
info()    { echo "   $*"; }
ok()      { echo "   ${G}OK${N}    $*";      SUMMARY+=("${G}OK${N}      $*"); }
warn()    { echo "   ${Y}WARN${N}  $*";      SUMMARY+=("${Y}WARN${N}    $*"); }
fail()    { echo "   ${R}FAIL${N}  $*";      SUMMARY+=("${R}MISSING${N} $*"); NFAIL=$((NFAIL+1)); }

ver_ge() { [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" == "$2" ]]; }   # $1 >= $2

SUDO_READY=0
need_sudo() {
  [[ $SUDO_READY -eq 1 ]] && return 0
  info "sudo is needed for this step."
  sudo -v || { echo "${R}sudo failed, stopping.${N}"; exit 1; }
  SUDO_READY=1
}

#----------------------------------------------------------------------------- platform
section "Platform"
ARCH=$(uname -m)
KREL=$(uname -r)
. /etc/os-release 2>/dev/null
info "${PRETTY_NAME:-unknown OS}, kernel $KREL, $ARCH"
if ! command -v apt-get >/dev/null; then
  echo "${R}This script supports Debian/Ubuntu (apt) only.${N}"; exit 1
fi
case "$ARCH" in
  x86_64)  CAEN_INSTALLERS=(install_x64 install_64) ;;
  aarch64) CAEN_INSTALLERS=(install_arm64 install_aarch64 install_arm) ;;
  *) echo "${R}Unsupported architecture $ARCH.${N}"; exit 1 ;;
esac
[[ $CHECK_ONLY -eq 1 ]] && info "check-only mode: nothing will be changed"

#----------------------------------------------------------------------------- apt packages
section "System packages"
pkg_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }
pkg_available() { apt-cache policy "$1" 2>/dev/null | grep -q "Candidate: [^(]"; }

CHARTS_PKG=qt6-charts-dev                                  # README's libqt6charts6-dev is the old name
pkg_available "$CHARTS_PKG" || CHARTS_PKG=libqt6charts6-dev
PKGS=(build-essential qt6-base-dev "$CHARTS_PKG" libcurl4-openssl-dev gdb libusb-1.0-0)   # CAENVMELib 4 installer needs libusb
[[ $DO_VENV -eq 1 ]]   && PKGS+=(python3-venv)
[[ $DO_DRIVER -eq 1 ]] && PKGS+=(dkms "linux-headers-$KREL" mokutil)

MISSING_PKGS=()
for p in "${PKGS[@]}"; do pkg_installed "$p" || MISSING_PKGS+=("$p"); done

if [[ ${#MISSING_PKGS[@]} -eq 0 ]]; then
  ok "apt packages: ${PKGS[*]}"
elif [[ $CHECK_ONLY -eq 1 ]]; then
  fail "apt packages not installed: ${MISSING_PKGS[*]}"
else
  info "installing: ${MISSING_PKGS[*]}"
  need_sudo
  sudo apt-get update -qq && sudo apt-get install -y "${MISSING_PKGS[@]}"
  STILL=(); for p in "${MISSING_PKGS[@]}"; do pkg_installed "$p" || STILL+=("$p"); done
  if [[ ${#STILL[@]} -eq 0 ]]; then ok "apt packages: ${PKGS[*]}"; else fail "apt could not install: ${STILL[*]}"; fi
fi

#----------------------------------------------------------------------------- CAEN libraries
section "CAEN libraries (from $CAEN_DIR)"

lib_version() {   # installed version of /usr/lib/lib$1.so, empty if none
  local f; f=$(readlink -f "/usr/lib/lib$1.so" 2>/dev/null)
  [[ -n "$f" && -e "$f" ]] && ldconfig -p | grep -q "lib$1.so" && basename "$f" | sed -E "s/^lib$1\.so\.?v?//"   # 2021: .so.1.5.0, now: .so.v1.8.0
}
find_tarball() {  # newest download for package $1
  find "$CAEN_DIR" -maxdepth 1 -type f \( -iname "$1-*.tgz" -o -iname "$1-*.tar.gz" \) 2>/dev/null | sort -V | tail -1
}
tarball_version() { basename "$1" | sed -E "s/^$2-v?([0-9]+(\.[0-9]+)*).*/\1/"; }
extract() {       # extract tarball $1 into a fresh $CAEN_DIR/.build/$2, print that directory
  local d="$CAEN_DIR/.build/$2"
  rm -rf "$d" && mkdir -p "$d" && tar xzf "$1" -C "$d" && echo "$d"
}

# lib name : package name : header : minimum version, in install order (each needs the one before)
CAEN_LIBS=("CAENVME:CAENVMELib:CAENVMElib.h:$MIN_CAENVME"
           "CAENComm:CAENComm:CAENComm.h:$MIN_CAENComm"
           "CAENDigitizer:CAENDigitizer:CAENDigitizer.h:$MIN_CAENDigitizer")
CAEN_LIBS_OK=1
for entry in "${CAEN_LIBS[@]}"; do
  IFS=: read -r lib pkg header minver <<< "$entry"
  have=$(lib_version "$lib")

  if [[ -n "$have" && -f "/usr/include/$header" ]]; then
    if ver_ge "$have" "$minver"; then ok "$pkg $have"; else warn "$pkg $have is older than the README minimum $minver"; fi
    continue
  fi

  tgz=$(find_tarball "$pkg")
  if [[ -z "$tgz" ]]; then
    fail "$pkg not installed, and no $pkg-*.tgz in $CAEN_DIR (download: $(caen_url "$pkg"))"
    CAEN_LIBS_OK=0; continue
  fi
  tver=$(tarball_version "$tgz" "$pkg")
  if [[ $CHECK_ONLY -eq 1 ]]; then
    fail "$pkg not installed (will install $(basename "$tgz"))"
    ver_ge "$tver" "$minver" || warn "$(basename "$tgz") is older than the README minimum $pkg $minver"
    CAEN_LIBS_OK=0; continue
  fi
  if [[ $CAEN_LIBS_OK -eq 0 ]]; then
    fail "$pkg not installed: install the libraries above it first"; continue
  fi

  info "installing $(basename "$tgz")"
  ver_ge "$tver" "$minver" || warn "$pkg $tver is older than the README minimum $minver"
  dir=$(extract "$tgz" "$pkg") || { fail "$pkg: could not extract $tgz"; CAEN_LIBS_OK=0; continue; }
  inst=""
  for name in "${CAEN_INSTALLERS[@]}"; do
    inst=$(find "$dir" -maxdepth 3 -type f -name "$name" | head -1); [[ -n "$inst" ]] && break
  done
  if [[ -z "$inst" ]]; then
    fail "$pkg: no ${CAEN_INSTALLERS[*]} script in $(basename "$tgz")"; CAEN_LIBS_OK=0; continue
  fi
  need_sudo
  (cd "$(dirname "$inst")" && sudo sh "./$(basename "$inst")")
  # CAEN's installers exit 0 even when they install nothing (e.g. CAENDigitizer without CAENComm),
  # so check the result rather than the exit code.
  have=$(lib_version "$lib")
  if [[ -n "$have" && -f "/usr/include/$header" ]]; then
    ok "$pkg $have (installed now)"
  else
    fail "$pkg: installer ran but lib$lib.so / $header are not in /usr/lib, /usr/include"; CAEN_LIBS_OK=0
  fi
done

#----------------------------------------------------------------------------- CAEN USB driver
DRIVER_PENDING_REBOOT=0
if [[ $DO_DRIVER -eq 1 ]]; then
  section "CAEN USB driver (CAENUSBdrvB, needed for DT57xx on USB)"
  SB_ON=0
  command -v mokutil >/dev/null && mokutil --sb-state 2>/dev/null | grep -q "SecureBoot enabled" && SB_ON=1
  [[ $SB_ON -eq 1 ]] && info "Secure Boot is on: the driver must be signed by a key enrolled in MOK"

  # Certificate DKMS signs modules with (Ubuntu sets it in framework.conf; dkms 3 default otherwise)
  dkms_cert() {
    local c
    c=$(grep -hsE '^\s*mok_certificate=' /etc/dkms/framework.conf /etc/dkms/framework.conf.d/*.conf | tail -1 | cut -d= -f2- | tr -d '"')
    for f in "$c" /var/lib/shim-signed/mok/MOK.der /var/lib/dkms/mok.pub; do [[ -n "$f" && -f "$f" ]] && { echo "$f"; return; }; done
  }
  key_enrolled() { local c; c=$(dkms_cert); [[ -n "$c" ]] && mokutil --test-key "$c" 2>&1 | grep -q "already enrolled"; }

  driver_status() {
    if lsmod | grep -q '^CAENUSBdrvB '; then echo loaded
    elif modinfo CAENUSBdrvB >/dev/null 2>&1; then echo installed
    else echo absent; fi
  }
  st=$(driver_status)

  if [[ $st == absent ]]; then
    tgz=$(find_tarball CAENUSBdrvB)
    if [[ -z "$tgz" ]]; then
      fail "CAENUSBdrvB not installed for kernel $KREL, and no CAENUSBdrvB-*.tgz in $CAEN_DIR (download: $(caen_url CAENUSBdrvB))"
    elif [[ $CHECK_ONLY -eq 1 ]]; then
      fail "CAENUSBdrvB not installed for kernel $KREL (will build $(basename "$tgz") with DKMS)"
      ver_ge "$(tarball_version "$tgz" CAENUSBdrvB)" "$MIN_USBDRV" || warn "$(basename "$tgz") is older than the README minimum CAENUSBdrvB $MIN_USBDRV"
    else
      tver=$(tarball_version "$tgz" CAENUSBdrvB)
      ver_ge "$tver" "$MIN_USBDRV" || warn "CAENUSBdrvB $tver is older than the README minimum $MIN_USBDRV"
      dir=$(extract "$tgz" CAENUSBdrvB)
      src=$(dirname "$(find "$dir" -name CAENUSBdrvB.c | head -1)")
      # Kernel 6.4 made DEFINE_SEMAPHORE take an initial count; CAEN's 1.5.4 source still uses
      # the one-argument form and does not compile on newer kernels (checked on 6.17).
      if grep -q '^static DEFINE_SEMAPHORE (minor_table_mutex);' "$src/CAENUSBdrvB.c"; then
        info "patching DEFINE_SEMAPHORE for kernel >= 6.4"
        sed -i 's/^static DEFINE_SEMAPHORE (minor_table_mutex);/#if LINUX_VERSION_CODE >= VERSION(6,4,0)\nstatic DEFINE_SEMAPHORE (minor_table_mutex, 1);\n#else\nstatic DEFINE_SEMAPHORE (minor_table_mutex);\n#endif/' "$src/CAENUSBdrvB.c"
      fi
      # DKMS rather than CAEN's "make install": it rebuilds the module on every kernel update
      # and signs it for Secure Boot. (CAEN's install target also appends to /etc/modules on
      # every run.) MAKE calls kbuild directly: DKMS appends KERNELRELEASE=..., which makes
      # CAEN's Makefile drop its default target, so a plain "make" ran its install target.
      cat > "$src/dkms.conf" <<EOF
PACKAGE_NAME="CAENUSBdrvB"
PACKAGE_VERSION="$tver"
BUILT_MODULE_NAME[0]="CAENUSBdrvB"
DEST_MODULE_LOCATION[0]="/updates/dkms"
MAKE[0]="make -C \${kernel_source_dir} M=\${dkms_tree}/CAENUSBdrvB/$tver/build LDDINCDIR=\${dkms_tree}/CAENUSBdrvB/$tver/build modules"
CLEAN="make clean"
AUTOINSTALL="yes"
EOF
      need_sudo
      sudo dkms remove -m CAENUSBdrvB -v "$tver" --all >/dev/null 2>&1    # leftovers of a failed run
      sudo rm -rf "/usr/src/CAENUSBdrvB-$tver"
      sudo cp -r "$src" "/usr/src/CAENUSBdrvB-$tver"
      if sudo dkms add -m CAENUSBdrvB -v "$tver" && sudo dkms install -m CAENUSBdrvB -v "$tver"; then
        info "CAENUSBdrvB $tver built and installed with DKMS"
      else
        fail "CAENUSBdrvB $tver did not build; see /var/lib/dkms/CAENUSBdrvB/$tver/build/make.log"
      fi
    fi
    st=$(driver_status)
  fi

  if [[ $st != absent ]]; then
    # Device node: on current kernels the driver's "usb/v1718_%d" name lands at /dev/v1718_N,
    # but CAENComm opens /dev/usb/v1718_N, so the SYMLINK is required (CAEN's own rule has it).
    # Permissions: plugdev instead of CAEN's world-writable 0666.
    RULE='KERNEL=="v1718_[0-9]*", SUBSYSTEM=="usbmisc", SYMLINK+="usb/%k", GROUP="plugdev", MODE="0660"'
    RULES_FILE=/etc/udev/rules.d/10-CAEN-USB.rules
    if [[ "$(cat "$RULES_FILE" 2>/dev/null)" != "$RULE" ]]; then
      if [[ $CHECK_ONLY -eq 1 ]]; then
        fail "udev rule $RULES_FILE missing or out of date"
      else
        need_sudo
        echo "$RULE" | sudo tee "$RULES_FILE" >/dev/null
        sudo udevadm control --reload-rules
        sudo udevadm trigger --subsystem-match=usbmisc --action=add   # apply to a board already plugged in
        sudo udevadm settle
        info "wrote $RULES_FILE"
      fi
    fi

    if [[ $st == installed && $CHECK_ONLY -eq 0 ]]; then
      need_sudo
      err=$(sudo modprobe CAENUSBdrvB 2>&1)
      [[ -n "$err" ]] && info "modprobe: $err"
      sudo udevadm trigger --subsystem-match=usbmisc --action=add
      st=$(driver_status)
    fi

    if [[ $st == loaded ]]; then
      dver=$(dkms status CAENUSBdrvB 2>/dev/null | sed -nE 's#^CAENUSBdrvB/([^,]+),.*#\1#p' | head -1)   # the module has no MODULE_VERSION
      ok "CAENUSBdrvB ${dver:-?} loaded"
    elif [[ $SB_ON -eq 1 ]] && ! key_enrolled; then
      cert=$(dkms_cert)
      if [[ $CHECK_ONLY -eq 1 || -z "$cert" ]]; then
        fail "CAENUSBdrvB installed but cannot load: Secure Boot key ${cert:-<none>} is not enrolled"
      else
        info "enrolling the DKMS signing key $cert in MOK."
        info "Choose a one-time password; you will type it once at the next boot."
        need_sudo
        if sudo mokutil --import "$cert"; then
          DRIVER_PENDING_REBOOT=1
          warn "CAENUSBdrvB will load after a reboot that enrolls the Secure Boot key (see below)"
        else
          fail "mokutil --import $cert failed"
        fi
      fi
    else
      fail "CAENUSBdrvB is installed but not loaded (sudo modprobe CAENUSBdrvB; check dmesg)"
    fi
  fi
fi

#----------------------------------------------------------------------------- build
if [[ $DO_BUILD -eq 1 ]]; then
  section "Build FSUDAQ_Qt6"
  QMAKE=""
  for q in qmake6 /usr/lib/qt6/bin/qmake; do
    q=$(command -v "$q" 2>/dev/null) || continue
    [[ "$("$q" -query QT_VERSION 2>/dev/null)" == 6.* ]] && { QMAKE=$q; break; }
  done

  if [[ -z "$QMAKE" ]]; then
    fail "no Qt 6 qmake (qmake6); install qt6-base-dev"
  elif [[ -z "$(lib_version CAENDigitizer)" || ! -f /usr/include/CAENDigitizer.h ]]; then
    fail "FSUDAQ_Qt6 not built: CAENDigitizer is not installed"
  elif [[ $CHECK_ONLY -eq 1 ]]; then
    if [[ -x FSUDAQ_Qt6 ]] && ! ldd FSUDAQ_Qt6 | grep -q "not found"; then ok "FSUDAQ_Qt6 is built"
    else fail "FSUDAQ_Qt6 not built (ready to build with $QMAKE)"; fi
  else
    # A Makefile from another qmake (e.g. Qt 5) leaves objects built against the wrong Qt.
    if [[ -f Makefile ]] && ! grep -q "^QMAKE *= *$QMAKE\$" Makefile; then
      info "Makefile was generated by a different qmake; cleaning"
      make clean >/dev/null 2>&1
    fi
    info "$QMAKE ($("$QMAKE" -query QT_VERSION)), make -j$(nproc); log in log/build.log"
    mkdir -p log
    if "$QMAKE" FSUDAQ_Qt6.pro > log/build.log 2>&1 && make -j"$(nproc)" >> log/build.log 2>&1; then
      missing=$(ldd FSUDAQ_Qt6 | grep "not found")
      if [[ -n "$missing" ]]; then fail "FSUDAQ_Qt6 built, but libraries are not found:"; echo "$missing"
      else ok "FSUDAQ_Qt6 built ($FSUDAQ_DIR/FSUDAQ_Qt6)"; fi
    else
      fail "build failed; last lines of log/build.log:"; grep -E "error|Error" log/build.log | tail -15
    fi
  fi
fi

#----------------------------------------------------------------------------- dashboard venv
if [[ $DO_VENV -eq 1 ]]; then
  section "Python venv for the online dashboard (online/venv)"
  VPY=online/venv/bin/python
  if [[ -x $VPY ]] && $VPY -c "import numpy, numba" 2>/dev/null; then
    ok "online/venv: numpy $($VPY -c 'import numpy; print(numpy.__version__)'), numba $($VPY -c 'import numba; print(numba.__version__)')"
  elif [[ $CHECK_ONLY -eq 1 ]]; then
    fail "online/venv with numpy + numba not set up"
  else
    [[ -x $VPY ]] || python3 -m venv online/venv
    if $VPY -m pip install -q --upgrade pip numpy numba; then
      ok "online/venv: numpy $($VPY -c 'import numpy; print(numpy.__version__)'), numba $($VPY -c 'import numba; print(numba.__version__)')"
    else
      fail "pip could not install numpy/numba into online/venv"
    fi
  fi
fi

#----------------------------------------------------------------------------- hardware
section "Digitizers"
NUSB=$(lsusb -d 21e1: 2>/dev/null | wc -l)
if [[ $NUSB -gt 0 ]]; then
  lsusb -d 21e1: | sed 's/^/   /'
  if [[ $DO_DRIVER -eq 1 ]]; then
    nodes=(/dev/usb/v1718_*)
    if [[ -e "${nodes[0]}" ]]; then
      for n in "${nodes[@]}"; do
        if [[ -r $n && -w $n ]]; then ok "$n is accessible"; else warn "$n exists but $USER cannot open it (in group plugdev? log out and in)"; fi
      done
    elif compgen -G "/dev/v1718_*" >/dev/null; then
      warn "$(echo /dev/v1718_*) exists but not /dev/usb/v1718_*, which CAENComm opens (udev rule SYMLINK missing)"
    elif [[ $DRIVER_PENDING_REBOOT -eq 0 ]]; then
      warn "CAEN USB device present but no /dev/usb/v1718_* node (driver not loaded?)"
    fi
  fi
else
  info "no CAEN USB device connected (fine for optical link / A4818)"
fi

#----------------------------------------------------------------------------- summary
section "Summary"
for s in "${SUMMARY[@]}"; do echo "   $s"; done
if [[ $DRIVER_PENDING_REBOOT -eq 1 ]]; then
  echo
  echo "${Y}${B}Reboot needed to enroll the Secure Boot key (someone must be at the console):${N}"
  echo "   1. sudo reboot"
  echo "   2. On the blue MOK Manager screen: Enroll MOK -> Continue -> Yes -> type the password -> Reboot"
  echo "   3. Run ./install.sh --check again"
fi
echo
if [[ $NFAIL -eq 0 ]]; then
  echo "${G}${B}Ready.${N} Start FSUDAQ with ./FSUDAQ (or ./FSUDAQ-gdb to capture a backtrace on a crash)."
  exit 0
else
  echo "${R}${B}$NFAIL item(s) missing.${N}"
  [[ $CHECK_ONLY -eq 1 ]] && echo "Run ./install.sh to install them."
  exit 1
fi

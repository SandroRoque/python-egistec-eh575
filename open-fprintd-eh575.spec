Name:           open-fprintd-eh575
Version:        0.3.0
Release:        3%{?dist}
Summary:        Egis EH575 Fingerprint Driver and Open Fprintd Manager

License:        MIT
URL:            https://github.com/SandroRoque/python-egistec-eh575
Source0:        %{url}/releases/download/v%{version}/%{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  systemd-rpm-macros

# Runtime Dependencies (Fedora naming convention)
Requires:       python3
Requires:       python3-dbus
Requires:       python3-gobject
Requires:       python3-numpy
Requires:       python3-opencv
Requires:       python3-scikit-image
Requires:       python3-pyusb
Requires:       polkit
Requires:       dbus-common

%description
An open-source implementation of the Fprintd manager and driver for
Egis EH575 fingerprint sensors. Installs to /opt/egis-driver to ensure stability.

%prep
%autosetup

%build
# No build step needed for pure python scripts in this manual installation mode
true

%install
# --- 1. Install to /opt/egis-driver (Matches PKGBUILD) ---
install -d %{buildroot}/opt/egis-driver
cp -r bin/open-fprintd bin/egis-bridge bin/egis-calibrate %{buildroot}/opt/egis-driver/
cp -r openfprintd egis_driver %{buildroot}/opt/egis-driver/

# Set executable permissions
chmod 755 %{buildroot}/opt/egis-driver/open-fprintd
chmod 755 %{buildroot}/opt/egis-driver/egis-bridge
chmod 755 %{buildroot}/opt/egis-driver/egis-calibrate

# --- 2. Install Systemd Services ---
install -d %{buildroot}%{_unitdir}
install -m 0644 open-fprintd.service %{buildroot}%{_unitdir}/
install -m 0644 egis-bridge.service %{buildroot}%{_unitdir}/
install -m 0644 egis-sleep-recovery.service %{buildroot}%{_unitdir}/

# --- 3. Install Polkit Policy ---
install -d %{buildroot}%{_datadir}/polkit-1/actions
install -m 0644 net.reactivated.fprint.policy %{buildroot}%{_datadir}/polkit-1/actions/

# --- 4. Install DBus Configuration ---
install -d %{buildroot}%{_datadir}/dbus-1/system.d
install -m 0644 io.github.uunicorn.Fprint.Device.Egis.conf %{buildroot}%{_datadir}/dbus-1/system.d/

# --- 5. Install Udev Rule ---
install -d %{buildroot}%{_udevrulesdir}
install -m 0644 70-egis-eh575.rules %{buildroot}%{_udevrulesdir}/

# --- 6. Create State Directory ---
# Note: Your python code uses /var/lib/open-fprintd/egis, not the /opt one in the old PKGBUILD
install -d %{buildroot}%{_sharedstatedir}/open-fprintd/egis
install -d %{buildroot}%{_sharedstatedir}/open-fprintd/egis-calibration

%post
%systemd_post open-fprintd.service egis-bridge.service egis-sleep-recovery.service
# Reload udev rules
udevadm control --reload-rules && udevadm trigger || :

%preun
%systemd_preun open-fprintd.service egis-bridge.service egis-sleep-recovery.service

%postun
%systemd_postun_with_restart open-fprintd.service egis-bridge.service egis-sleep-recovery.service

%files
%license LICENSE
# Main Application Files
/opt/egis-driver/

# Systemd Units
%{_unitdir}/open-fprintd.service
%{_unitdir}/egis-bridge.service
%{_unitdir}/egis-sleep-recovery.service

# Configs
%{_datadir}/polkit-1/actions/net.reactivated.fprint.policy
%{_datadir}/dbus-1/system.d/io.github.uunicorn.Fprint.Device.Egis.conf
%{_udevrulesdir}/70-egis-eh575.rules

# State Directory (Owned by package)
%dir %{_sharedstatedir}/open-fprintd
%dir %{_sharedstatedir}/open-fprintd/egis
%dir %{_sharedstatedir}/open-fprintd/egis-calibration

%changelog
* Sun Jun 07 2026 Sandro Roque <SandroRoque@users.noreply.github.com> - 0.3.0-1
- Public fork release with EH575 matching, calibration, and lock-screen integration

* Sat Jan 24 2026 Jayabbhi <abbhinavjayaraman@gmail.com> - 0.2.5-1
- Updated for isolated /opt installation

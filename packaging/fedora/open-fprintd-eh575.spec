Name:           open-fprintd-eh575
Version:        0.4.0
Release:        1%{?dist}
Summary:        Egis EH575 fingerprint driver and open-fprintd manager
License:        MIT AND GPL-2.0-only
URL:            https://github.com/SandroRoque/python-egistec-eh575
Source0:        %{url}/releases/download/v%{version}/%{name}-%{version}.tar.gz
BuildArch:      noarch
BuildRequires:  systemd-rpm-macros
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
Open-source userspace driver for the EgisTec EH575 USB fingerprint sensor,
including an open-fprintd-compatible manager and backend bridge.

%prep
%autosetup -n %{name}-%{version}

%build
true

%install
project="%{_builddir}/%{name}-%{version}/open-fprintd-eh575"
install -d %{buildroot}/opt/egis-driver
install -m 0755 "$project/bin/open-fprintd" %{buildroot}/opt/egis-driver/open-fprintd
install -m 0755 "$project/bin/egis-bridge" %{buildroot}/opt/egis-driver/egis-bridge
install -m 0755 "$project/bin/egis-calibrate" %{buildroot}/opt/egis-driver/egis-calibrate
install -m 0755 %{_builddir}/%{name}-%{version}/egis-doctor %{buildroot}/opt/egis-driver/egis-doctor
install -d %{buildroot}%{_bindir}
ln -s ../../opt/egis-driver/egis-doctor %{buildroot}%{_bindir}/egis-doctor
cp -r "$project/openfprintd" "$project/egis_driver" %{buildroot}/opt/egis-driver/
install -D -m 0644 "$project/open-fprintd.service" %{buildroot}%{_unitdir}/open-fprintd.service
install -D -m 0644 "$project/egis-bridge.service" %{buildroot}%{_unitdir}/egis-bridge.service
install -D -m 0644 "$project/net.reactivated.fprint.policy" \
  %{buildroot}%{_datadir}/polkit-1/actions/net.reactivated.fprint.policy
install -D -m 0644 "$project/io.github.uunicorn.Fprint.Device.Egis.conf" \
  %{buildroot}%{_datadir}/dbus-1/system.d/io.github.uunicorn.Fprint.Device.Egis.conf
install -D -m 0644 "$project/70-egis-eh575.rules" \
  %{buildroot}%{_udevrulesdir}/70-egis-eh575.rules
install -d -m 0700 %{buildroot}%{_sharedstatedir}/open-fprintd/egis
install -d -m 0700 %{buildroot}%{_sharedstatedir}/open-fprintd/egis-calibration

%post
%systemd_post open-fprintd.service egis-bridge.service
udevadm control --reload-rules && udevadm trigger || :

%preun
%systemd_preun open-fprintd.service egis-bridge.service

%postun
%systemd_postun_with_restart open-fprintd.service egis-bridge.service

%files
%license LICENSE open-fprintd-eh575/openfprintd/COPYING
/opt/egis-driver/
%{_bindir}/egis-doctor
%{_unitdir}/open-fprintd.service
%{_unitdir}/egis-bridge.service
%{_datadir}/polkit-1/actions/net.reactivated.fprint.policy
%{_datadir}/dbus-1/system.d/io.github.uunicorn.Fprint.Device.Egis.conf
%{_udevrulesdir}/70-egis-eh575.rules
%dir %{_sharedstatedir}/open-fprintd
%dir %{_sharedstatedir}/open-fprintd/egis
%dir %{_sharedstatedir}/open-fprintd/egis-calibration

%changelog
* Mon Jul 13 2026 Sandro Roque <SandroRoque@users.noreply.github.com> - 0.4.0-1
- Reproducible EH575 release with validated matching and lifecycle handling

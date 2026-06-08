# Maintainer: <Your Name> <your@email.com>
pkgname=open-fprintd-eh575
pkgver=0.3.0
pkgrel=1
pkgdesc="Egis EH575 Fingerprint Driver and Open Fprintd Manager (Isolated /opt Install)"
arch=('any')
url="https://github.com/SandroRoque/python-egistec-eh575"
license=('MIT')
depends=('python' 'python-dbus' 'python-gobject' 'python-numpy' 'python-opencv' 'python-scikit-image' 'python-pyusb' 'polkit' 'dbus')
makedepends=('git')
conflicts=('open-fprintd-eh575-git')
source=("git+https://github.com/SandroRoque/python-egistec-eh575.git")
sha256sums=('SKIP')

package() {
  cd "${srcdir}/python-egistec-eh575/open-fprintd-eh575"

  local _optdir="$pkgdir/opt/egis-driver"
  install -d "$_optdir"

  cp -r bin/open-fprintd bin/egis-bridge bin/egis-calibrate "$_optdir/"
  cp -r openfprintd egis_driver "$_optdir/"

  chmod 755 "$_optdir/open-fprintd"
  chmod 755 "$_optdir/egis-bridge"
  chmod 755 "$_optdir/egis-calibrate"

  install -d -m 0700 "$pkgdir/var/lib/open-fprintd/egis"
  install -d -m 0700 "$pkgdir/var/lib/open-fprintd/egis-calibration"

  install -d "$pkgdir/usr/lib/systemd/system"
  install -m 0644 open-fprintd.service "$pkgdir/usr/lib/systemd/system/"
  install -m 0644 egis-bridge.service "$pkgdir/usr/lib/systemd/system/"

  install -D -m 0644 net.reactivated.fprint.policy "$pkgdir/usr/share/polkit-1/actions/net.reactivated.fprint.policy"
  install -D -m 0644 io.github.uunicorn.Fprint.Device.Egis.conf "$pkgdir/usr/share/dbus-1/system.d/io.github.uunicorn.Fprint.Device.Egis.conf"
  install -D -m 0644 70-egis-eh575.rules "$pkgdir/usr/lib/udev/rules.d/70-egis-eh575.rules"
}

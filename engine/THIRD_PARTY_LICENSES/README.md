# Python runtime license sources

This directory ships with Goblin at
`Contents/Resources/Runtime/THIRD_PARTY_LICENSES`. It contains the complete
license text for each third-party component that Goblin redistributes from the
bundled CPython runtime.

## Runtime pin

| Field | Value |
| --- | --- |
| Project | python-build-standalone |
| Build | 20261013 |
| Tag commit | 9e40cba825ca0997a01cab9f60a0b15ffb976af2 |
| Asset | cpython-3.14.8+20261013-aarch64-apple-darwin-install_only_stripped.tar.gz |
| Archive SHA-256 | 994adadc202c5df0b4570192cc9a70e3f2c131e512f4a125c56efa8562f44b28 |
| URL | https://github.com/astral-sh/python-build-standalone/releases/download/20261013/cpython-3.14.8%2B20261013-aarch64-apple-darwin-install_only_stripped.tar.gz |

The matching full archive used for build metadata and upstream license texts:

| Field | Value |
| --- | --- |
| Asset | cpython-3.14.8+20261013-aarch64-apple-darwin-pgo+lto-full.tar.zst |
| Archive SHA-256 | 5806ddb9d95d73796c87e1aeb45bf7ec984d08ae9eb94f9020436052b32ce498 |
| URL | https://github.com/astral-sh/python-build-standalone/releases/download/20261013/cpython-3.14.8%2B20261013-aarch64-apple-darwin-pgo%2Blto-full.tar.zst |

Versions and source archive checksums below come from
`pythonbuild/downloads.json` at the python-build-standalone `20261013` tag.
The `python/PYTHON.json` file in the full archive records each extension's
static library and license path.

## Redistributed components

| Component | Version | License | Redistributed as | License text |
| --- | --- | --- | --- | --- |
| CPython | 3.14.8 | PSF-2.0 and CNRI-Python | bin/python3.14 and lib/libpython3.14.dylib | CPython-3.14.8.txt |
| OpenSSL | 3.5.9 | Apache-2.0 | static in built-in _ssl and _hashlib | OpenSSL-3.5.9.txt |
| SQLite | 3.53.1 | Public domain | static in built-in _sqlite3 | SQLite-3.53.1.txt |
| Expat | 2.9.0 | MIT | static in built-in pyexpat | Expat-2.9.0.txt |
| libffi | 3.8.0 | MIT | static in built-in _ctypes | libffi-3.8.0.txt |
| bzip2 | 1.0.8 | bzip2-1.0.6 | static in built-in _bz2 | bzip2-1.0.8.txt |
| liblzma | 5.8.4 | 0BSD | static in built-in _lzma | liblzma-5.8.4.txt |
| mpdecimal | 4.0.0 | BSD-2-Clause | static in built-in _decimal | mpdecimal-4.0.0.txt |
| Zstandard | 1.5.7 | BSD-3-Clause | static in built-in _zstd | zstd-1.5.7.txt |
| libuuid | 1.0.3 | BSD-3-Clause | static in built-in _uuid | libuuid-1.0.3.txt |
| HACL* | 8ba599b2f6c9701b3dc961db895b0856a2210f76 | Apache-2.0 | static in built-in _sha1, _sha2, _sha3, _md5, _blake2 and _hmac | HACL-star-8ba599b2f6c9.txt |

## License text checksums

| File | SHA-256 |
| --- | --- |
| CPython-3.14.8.txt | 86e61415828a8b5b06ec8d024e6f086ce155a8b85fd0c419c0ba4dc004e74fdd |
| OpenSSL-3.5.9.txt | 7d5450cb2d142651b8afa315b5f238efc805dad827d91ba367d8516bc9d49e7a |
| SQLite-3.53.1.txt | 38bef3d28b24f145ea293bd3b6eb4b20396982abc8303128fb493986ea5bc719 |
| Expat-2.9.0.txt | 122f2c27000472a201d337b9b31f7eb2b52d091b02857061a8880371612d9534 |
| libffi-3.8.0.txt | deaf3a42effb551a5b140fa9afefed183a27f1341c6d1bf430d106a5e6931fc0 |
| bzip2-1.0.8.txt | 1f38bbc7caacafd65169276d759c0d88c991b753b643ce35d0e45ea1971dd441 |
| liblzma-5.8.4.txt | 9a4062de0a2c388a98cf35a35d348b62fa97c838a71c3c28ee1a2d7d0a565b02 |
| mpdecimal-4.0.0.txt | 669512af7219f58be03a398766d7c9da11a3b3df9d3f05cb74c5ceca25c8da3b |
| zstd-1.5.7.txt | 7055266497633c9025b777c78eb7235af13922117480ed5c674677adc381c9d8 |
| libuuid-1.0.3.txt | 122ee1f7e258f2c3c0e538a75c037684f420454bf3850ddc74ce750bbf5fe86b |
| HACL-star-8ba599b2f6c9.txt | c5accbbd8546e94c34aed24afe689a617627d18eed5a6c48277e48db57c23851 |

## Build input source archives

| Component | Source | SHA-256 |
| --- | --- | --- |
| OpenSSL 3.5.9 | https://github.com/openssl/openssl/releases/download/openssl-3.5.9/openssl-3.5.9.tar.gz | 603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a |
| SQLite 3.53.1 | https://www.sqlite.org/2026/sqlite-autoconf-3530100.tar.gz | 83e6b2020a034e9a7ad4a72feea59e1ad52f162e09cbd26735a3ffb98359fc4f |
| Expat 2.9.0 | https://github.com/libexpat/libexpat/releases/download/R_2_9_0/expat-2.9.0.tar.xz | 1e6371862cc31999b368c3b89b49994f0677e1bab5f1b2b85ae3741f5d803051 |
| libffi 3.8.0 | https://github.com/libffi/libffi/releases/download/v3.8.0/libffi-3.8.0.tar.gz | 7da3e2d9a171eb0a038f592ecad3ff2bb2550f3496d87b3b29ad0cf4430c0db4 |
| bzip2 1.0.8 | https://sourceware.org/pub/bzip2/bzip2-1.0.8.tar.gz | ab5a03176ee106d3f0fa90e381da478ddae405918153cca248e682cd0c4a2269 |
| liblzma 5.8.4 | https://github.com/tukaani-project/xz/releases/download/v5.8.4/xz-5.8.4.tar.gz | 0014c7886930454fe8bd4228665b51af55eeae560ea135c9c4cd33f55b2591d9 |
| mpdecimal 4.0.0 | https://www.bytereef.org/software/mpdecimal/releases/mpdecimal-4.0.0.tar.gz | 942445c3245b22730fd41a67a7c5c231d11cb1b9936b9c0f76334fb7d0b4468c |
| Zstandard 1.5.7 | https://github.com/python/cpython-source-deps/archive/refs/tags/zstd-1.5.7.tar.gz | f24b52470d12f466e9fa4fcc94e6c530625ada51d7b36de7fdc6ed7e6f499c8e |
| libuuid 1.0.3 | https://sourceforge.net/projects/libuuid/files/libuuid-1.0.3.tar.gz | 46af3275291091009ad7f1b899de3d0cea0252737550e7919d17237997db5644 |
| HACL* 8ba599b2f6c9701b3dc961db895b0856a2210f76 | https://github.com/hacl-star/hacl-star/tree/8ba599b2f6c9701b3dc961db895b0856a2210f76 | Not published as a tarball by the pin. License taken from the pinned revision. |

## Source notes

- The full archive's `python/licenses` directory contains texts for all listed
  components except zstd. `PYTHON.json` names `licenses/LICENSE.zstd.txt`, but
  that file is absent from the `20261013` full archive. The zstd text here comes
  from the exact `zstd-1.5.7` source archive named by the build metadata and is
  byte-identical to its `LICENSE`.
- HACL* has no separate license path in `PYTHON.json`; CPython compiles the
  HACL* objects directly. The text here is from the upstream revision named by
  CPython 3.14.8 `Modules/_hacl/refresh.sh`.
- `PYTHON.json` labels `_sqlite3` with the Zlib identifier, but the build's
  `LICENSE.sqlite.txt` is SQLite's public-domain dedication. The public-domain
  text is the applicable license for the bundled SQLite code.
- `-lreadline` resolves to Apple's `/usr/lib/libedit.3.dylib`, and
  `readline.backend` reports `editline`. No GNU Readline library is
  redistributed.

## Not included in the app bundle

The runtime executable and `libpython3.14.dylib` link only Apple system
libraries: libSystem, libz, libedit, libncurses, libpanel, CoreFoundation and
SystemConfiguration. Those libraries are provided by macOS and are not
redistributed.

The release build removes pip, ensurepip, idle, idlelib, the pydoc command
wrappers, tkinter, _tkinter, Tcl, Tk, Itcl, Thread, test, turtledemo, config,
pkgconfig, include and share. They are in the upstream archive and developer
cache but not in the signed app. No license text for those pruned components is
shipped.

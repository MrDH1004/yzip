# Test data

The `.egg` and `.alz` samples come from the test suites of [unegg](https://github.com/alkegi/unegg) and
[unalz](https://github.com/alkegi/unalz) (tag 0.2.1), dedicated to the public domain under
[CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/). Encrypted samples use the password `test1234`.

Everything else test_moreformats.py builds itself (CAB with makecab, ISO with Windows' IMAPI2, and the Python-
decoded formats with small encoders in the test).

`libarchive_rar5_stored.rar.uu` is the small RAR5 fixture from libarchive's BSD-2-Clause test suite;
the test decodes the uuencoded fixture into a temporary file before opening it.
`libarchive_rar5_encrypted.rar.uu` uses the same suite's password `password` fixture.

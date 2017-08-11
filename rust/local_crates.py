# Copyright 2017 The Fuchsia Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

# This file lists the Rust crates hosted in the Fuchsia tree which have been
# published to crates.io. This ensures we use the local version and not the
# vendored version when compiling other crates.

import os

ROOT_PATH = os.path.abspath(__file__ + "/../../..")

RUST_CRATES = {
    # Fuchsia crates.
    "published": {
        "magenta": {
            "version": "0.1.1",
            "path": "rust/magenta-rs",
            "target": "//rust/magenta-rs:magenta",
        },
        "magenta-sys": {
            "version": "0.1.1",
            "path": "rust/magenta-rs/magenta-sys",
            "target": "//rust/magenta-rs/magenta-sys",
        },
    },
    # Third-party crates whose sources are mirrored.
    # The GN target for these will be automatically generated.
    "mirrors": {
        "mio": {
            "version": "0.6.10",
            "path": "third_party/rust-mirrors/mio",
        },
    },
}

def get_all_paths(relative=False):
    mapper = lambda (k, v): (v["path"] if relative
                             else os.path.join(ROOT_PATH, v["path"]))
    def extract_paths(crate_data): return map(mapper, crate_data.iteritems())
    return (extract_paths(RUST_CRATES["published"]) +
            extract_paths(RUST_CRATES["mirrors"]))
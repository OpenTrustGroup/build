# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
#
# Standalone build of a delivered source half, in the shape a consumer uses.
#
# This file ships *inside* each half and builds that half alone:
#
#   libddk/      the component, compiler included
#   grt_ddk/     one half of the sources, at their delivered paths, with a
#                copy of this file as its Makefile
#   mtk_ddk/     the other half, the same way
#
# It lives in the half because a half is what repo manages, and repo manages
# directories: a Makefile beside them was a file no project owned.  Each half
# builds on its own -- `make -C mtk_ddk` -- so a customer who receives one
# half builds it exactly the way MTK build both.  Nothing here reaches into
# the other half; that they are separately buildable is proved on every
# release by the split self-test.
#
# Sources keep their source-tree path on purpose.  The two halves of a split
# are then a partition of the tree, merging them is a union, and a file added
# on either side lands where its author put it.  A per-driver flattened
# directory would lose all three.
#
# There is no path back to the product tree, so a missing header or archive
# fails here instead of being satisfied quietly from //.  Do not add an
# include or library path outside this half and the component beside it: that
# would turn this into a build that cannot fail, which is the one thing it
# must not be.

# Paths are relative to the sandbox, which make is always invoked in.  That is
# not tidiness: __FILE__ carries whatever path the compiler was handed, so an
# absolute one bakes the sandbox's location into every object that asserts or
# logs.  Two builds of the same source in two directories would then differ,
# and a half MTK or a client returns could not be compared against ours.
#
# $(firstword): this file is the first makefile make reads, whatever else is
# named on the command line.
HALF_DIR  := $(patsubst %/,%,$(dir $(abspath $(firstword $(MAKEFILE_LIST)))))
HALF      := $(notdir $(HALF_DIR))
# The component sits beside the half, which is where a delivery puts it.
# Overridable because our own pipeline has to build a sandbox before the
# component it will eventually carry is finished.
LIBDDK    ?= ../libddk
BUILD     ?= build

# The compiler ships inside the component, so there is nothing to link in
# beside it and nothing for a consumer to keep in step.  Overridable on the
# command line for a sandbox built before the compiler is put in, which is
# what our own pipeline does: it has to build the delivery first.
TOOLCHAIN ?= $(LIBDDK)/toolchain

# The host tools carry their own libraries, in toolchain/lib/host, and the
# loader has to be pointed at them.  ld.lld is linked against libxml2.so.2 --
# for COFF manifests, which no link here touches -- and a machine whose
# libxml2 has moved on to another soname cannot start it at all.  Without
# this, two hundred files compile and the first link dies inside the loader,
# naming a library nobody asked for.
#
# Absolute, because a recipe's own working directory is the only thing the
# loader would resolve a relative entry against, and prepended, so what the
# delivery carries wins over whatever the machine has.
export LD_LIBRARY_PATH := $(abspath $(TOOLCHAIN))/lib/host$(if $(LD_LIBRARY_PATH),:$(LD_LIBRARY_PATH))

# The parts of a delivery travel separately -- one repository each -- and
# are not held to one release.  mtk_ddk is the half the customer works in,
# so its content moves with their changes rather than with our releases, and
# a half that did not change between releases is not re-cut for the next.
# What a half has to agree with the component on is the ABI and the
# toolchain, and the recipe checks those against libddk itself.
#
# A RELEASE that differs from the component's is still worth saying before
# anything is compiled: if the build then fails where the ABI moved, two
# hundred files of output are what make the real answer hard to see.  So it
# is named, and the build goes on.
_PARTS   := $(LIBDDK) .
# $(strip): foreach joins with spaces, so a run that matched nothing is a
# string of blanks, and ifneq calls that non-empty.
_STAMPED := $(strip $(foreach d,$(_PARTS),$(if $(wildcard $(d)/RELEASE),$(d))))
# Two to compare, or there is nothing to compare.  make has no arithmetic,
# so the two counts that mean "not enough" are named.
ifeq ($(filter 0 1,$(words $(_STAMPED))),)
RELEASES := $(sort $(foreach d,$(_STAMPED),$(shell cat $(d)/RELEASE)))
ifneq ($(words $(RELEASES)),1)
$(warning $(HALF) and $(LIBDDK) state different releases: \
  $(foreach d,$(_STAMPED),[$(d) $(shell cat $(d)/RELEASE)]). Building anyway; \
  if this fails where the ABI moved, sync the libddk that goes with these \
  sources)
endif
endif

CC  := $(TOOLCHAIN)/bin/clang
CXX := $(TOOLCHAIN)/bin/clang++

# A module description sits beside the sources it describes, so that a
# partition by path carries it to the same half as the code.  The module is
# named by its description, not by its directory: several modules legitimately
# share one.
MODULE_FILES := $(patsubst ./%,%,\
                  $(sort $(shell find . -name '*.ddk.mk' 2>/dev/null)))
MODULES      := $(patsubst %.ddk.mk,%,$(notdir $(MODULE_FILES)))

# A module's sources are named relative to the half, which is where make
# runs, so nothing has to remember which root they came from.
$(foreach f,$(MODULE_FILES),\
  $(eval MODULE_FILE_$(patsubst %.ddk.mk,%,$(notdir $(f))) := $(f)))

ifneq ($(words $(MODULES)),$(words $(sort $(MODULES))))
$(error two module descriptions claim the same name: $(MODULES))
endif

ifeq ($(MODULES),)
$(error no module description under $(HALF_DIR): a half says what it builds \
  in <module>.ddk.mk files beside the sources they describe)
endif

ARCH_FLAGS := --target=aarch64-fuchsia
SYSROOT    := $(LIBDDK)/sysroot

# include/lib as well as include/: the product reaches ulib/zx's headers
# through both, so <zx/time.h> and <lib/zx/time.h> name one file there, and
# the delivery publishes that file once, under lib/.  Without the second
# root the short spelling would find nothing.
#
# A half may carry headers and archives of its own, under libddk/ inside it.
# The component beside the halves holds what both of them need; what only one
# half needs travels in that half, so the other never receives it.  MTK's
# half has none of this and never sees the flag: $(wildcard) answers nothing
# and the $(if) drops the whole thing rather than emitting a bare -I, which
# would swallow the flag after it.
EXTRA_DDK  := $(patsubst %/include,%,$(wildcard libddk/include))
EXTRA_INCS := $(if $(EXTRA_DDK),-I$(EXTRA_DDK)/include -I$(EXTRA_DDK)/include/lib)
# The half's own sysroot headers, where the delivery gives it any: what only
# this half reads travels inside it, sysroot and all, and --sysroot names one
# directory.  -isystem, not -I: these answer <stdio.h> and <zircon/types.h>
# the way the sysroot does, and must not outrank the toolchain's own libc++
# -- -nostdlibinc once hid that and left <cmath> disagreeing with math.h.
EXTRA_SYSINCS := $(if $(EXTRA_DDK),$(if $(wildcard $(EXTRA_DDK)/sysroot/include),-isystem $(EXTRA_DDK)/sysroot/include))
# $(EXTRA_DDK) empty would make this $(wildcard /lib), which exists on the
# build host: the link line would quietly gain -L/lib and the sandbox would
# stop being sealed.  Guarded on EXTRA_DDK, not on the wildcard.
EXTRA_LIBS := $(if $(EXTRA_DDK),$(if $(wildcard $(EXTRA_DDK)/lib),-L$(EXTRA_DDK)/lib))

# The link is traced as well as the compile.  A dependency file records what
# the compiler read and nothing about what the linker opened, so without this
# every archive in the component looks unused -- which is exactly the wrong
# answer to give anyone deciding what to ship.
#
# -MD, not -MMD: -MMD omits system headers, and the sysroot arrives with the
# target, so the dependency files would record nothing from it.  The
# hermeticity check reads exactly those files, and a check that cannot see
# system headers cannot see a host header either.
#
# The host is kept out by cross-compiling, not by -nostdlibinc.  With
# --target=aarch64-fuchsia and an explicit --sysroot, clang searches the
# target's libc++, its own builtin headers and the sysroot -- and nothing
# else; check_hermetic.py confirms that against what the compiler actually
# read.  -nostdlibinc also hid the target's libc++, which left <cmath>
# disagreeing with the sysroot's math.h.
#
# Two spellings, because the sources use two for their own headers:
# root-relative, as in
# <garnet/public/lib/fxl/synchronization/thread_annotations.h>, and relative
# to the public library, as in <lib/fxl/logging.h>.  Those files are GRT's own
# sources and travel with a source half, not with libddk.
#
# Named outright rather than found: MTK's half carries no public library at
# all, and pointing -I at a directory that is not there is harmless, while a
# $(wildcard) that matched nothing would emit a bare `-I` and swallow the flag
# after it.
SRC_INCS := -I. -I./garnet/public
# Part of the delivery, not of this Makefile: a consumer that compiles with
# different settings here gets objects that do not link against the archives
# libddk ships, and the failure surfaces as missing typeinfo or a mangling
# mismatch rather than as a flag mismatch.  These are the product's own, read
# off the build it must stay compatible with.
ABI_DEFINES := \
	-D_ALL_SOURCE=1 \
	-D__Nebula__=1 \
	-DZX_DEBUGLEVEL=2 \
	-D_LIBCPP_ENABLE_THREAD_SAFETY_ANNOTATIONS \
	-DGOOGLE_PROTOBUF_NO_RTTI \
	-DGOOGLE_PROTOBUF_NO_STATIC_INITIALIZER \
	-DHAVE_PTHREAD

# The product's optimisation level, and for the same reason the defines
# above are: what ships is what the product tested.  Lost once -- the module
# descriptions carry a target's own flags and not the toolchain's, so the
# halves were built at -O0 and came out 1.8x the size of the product's
# binaries; on mt8668 that was 23 MB more bootfs in a 128 MB hypervisor and
# the system ran out of memory bringing the guest up.  EXTRA_CFLAGS still
# appends, so -O2 or -O0 on the command line wins.
OPT_FLAGS := -Og

COMMON_FLAGS := \
	$(ARCH_FLAGS) \
	--sysroot=$(SYSROOT) \
	$(OPT_FLAGS) \
	-I$(LIBDDK)/include \
	-I$(LIBDDK)/include/lib \
	$(EXTRA_INCS) \
	$(EXTRA_SYSINCS) \
	$(SRC_INCS) \
	-fPIC \
	-MD -MP \
	$(ABI_DEFINES)


# -fno-rtti and -fno-exceptions are ABI, not taste: libddk's C++ archives are
# built without either, so a consumer's objects must be too.  A module that
# needs exceptions says so in its own MODULE_CFLAGS, as its BUILD.gn does.
ABI_CXX_FLAGS := \
	-fno-exceptions \
	-fno-rtti \
	-fvisibility-inlines-hidden

# `override`, not `?=`.  These carry the ABI -- the target triple, the
# sysroot, -fno-rtti -- and they are the delivery's, not the caller's.  With
# `?=` an exported CFLAGS replaces them outright, which is not hypothetical:
# bitbake exports CFLAGS and LDFLAGS, so building this under Yocto silently
# dropped --target=aarch64-fuchsia and compiled for the host.  The failure
# then arrives as a mangling mismatch at link time, pointing at nothing.
#
# EXTRA_* below is the way in.  A consumer can add to the line; they cannot
# replace it, and the difference is the whole point.
override CFLAGS   := $(COMMON_FLAGS) -std=c11
override CXXFLAGS := $(COMMON_FLAGS) $(CXX_STDLIB_INC) $(ABI_CXX_FLAGS) -std=c++14
override LDFLAGS  := $(ARCH_FLAGS) --sysroot=$(SYSROOT) -shared -L$(LIBDDK)/lib $(EXTRA_LIBS) -L$(BUILD)/lib

# Appended after ours, so -O2 or -g works and --sysroot does not.
EXTRA_CFLAGS   ?=
EXTRA_CXXFLAGS ?=
EXTRA_LDFLAGS  ?=

# What the image carries is stripped, as the product's binaries are: the
# symbol tables are for a debugger, not for devhost or the loader, and they
# were half of what shipped -- a driver came out at 314 kB stripped to 264.
# Done by the linker rather than by a strip tool, because the delivered
# toolchain carries none; a build that wants the symbols in place clears
# this on the command line.
STRIP_FLAGS ?= -Wl,--strip-all

.PHONY: all clean list install list-install

# `all` is declared at the bottom, because make expands an explicit rule's
# prerequisites immediately and the module loops have not run yet.  That
# leaves `list` as the first rule, so a bare `make` would print module names,
# build nothing, and exit 0 -- which reads exactly like a successful build.
.DEFAULT_GOAL := all

list:
	@$(foreach m,$(MODULES),echo '$(HALF)	$(m)';)

clean:
	rm -rf $(BUILD)

# A packaging step needs somewhere to put things and a way to find out where
# they went.  DESTDIR is the staging root a distribution build sets;
# PREFIX is where the delivery sits inside the image.
DESTDIR ?=
PREFIX  ?= /ddk

install: all
	@$(foreach m,$(MODULES),\
	  mkdir -p $(DESTDIR)$(PREFIX)/$(dir $($(m)_INSTALL)) && \
	  install -m 0644 $($(m)_OUT) $(DESTDIR)$(PREFIX)/$($(m)_INSTALL) && \
	  echo '$(PREFIX)/$($(m)_INSTALL)';)

# What install would write, without writing it: a recipe's FILES line has to
# name these, and reading them out of the build beats keeping a second list
# in step with this one.
list-install:
	@$(foreach m,$(MODULES),echo '$(PREFIX)/$($(m)_INSTALL)';)

# A DDK delivery is not only drivers.  A consumer building outside our tree
# receives the virtual machine monitor, the guest manager and their libraries
# as sources too, so the standalone build has to produce every kind of target
# the product build does: loadable drivers, programs, shared and static
# libraries, and it has to let one of them link another.
LIBDIR := $(BUILD)/lib
BINDIR := $(BUILD)/bin

define module_rules
$(eval MODULE_NAME := $(1))
$(eval MODULE_TYPE := driver)
$(eval MODULE_DEPS :=)
$(eval MODULE_SRCS :=)
$(eval MODULE_INCLUDE_DIRS :=)
$(eval MODULE_LIBS :=)
$(eval MODULE_CFLAGS :=)
$(eval include $(MODULE_FILE_$(1)))

$(1)_NAME := $$(MODULE_NAME)
$(1)_OBJS := $$(patsubst %,$(BUILD)/$(1)/%.o,$$(basename $$(MODULE_SRCS)))
$(1)_LIBS := $$(MODULE_LIBS)
$(1)_DEPS := $$(MODULE_DEPS)

# A module's own directories go on the quoted-include path, not the angle
# one.  With -I they also answer <...> lookups and outrank the sysroot, and
# fsl/socket/strings.h then answers the sysroot's own #include <strings.h>.
# What a BUILD.gn declares is a real include path and stays on -I.
$(1)_INCS := $$(sort \
  $$(addprefix -iquote,$$(dir $$(MODULE_SRCS))) \
  $$(addprefix -I,$$(MODULE_INCLUDE_DIRS)))
$(1)_CFLAGS := $$(MODULE_CFLAGS)

# A module with any C++ source links with the C++ driver, so that the
# standard library comes in.  Linking C++ objects with the C driver leaves
# std::basic_string undefined and blames the module for it.
$(1)_LD := $$(if $$(filter %.cc %.cpp,$$(MODULE_SRCS)),$$(CXX),$$(CC))

ifeq ($$(MODULE_TYPE),driver)
$(1)_OUT := $(BUILD)/$(1)/module.so
else ifeq ($$(MODULE_TYPE),executable)
$(1)_OUT := $(BINDIR)/$$(MODULE_NAME)
else ifeq ($$(MODULE_TYPE),shared_library)
$(1)_OUT := $(LIBDIR)/lib$$(MODULE_NAME).so
else
$(1)_OUT := $(LIBDIR)/lib$$(MODULE_NAME).a
endif
$(1)_TYPE := $$(MODULE_TYPE)

ALL_OUTPUTS += $$($(1)_OUT)
MODULE_OUTPUT_$$(MODULE_NAME) := $$($(1)_OUT)

# Where this lands under `make install`.  The same layout combine_builds.py
# gives a binary bundle, so a half delivered as sources and a half delivered
# as binaries install to the same paths and a consumer's packaging does not
# have to know which it got.
ifeq ($$(MODULE_TYPE),driver)
$(1)_INSTALL := drivers/$$(MODULE_NAME).so
else ifeq ($$(MODULE_TYPE),executable)
$(1)_INSTALL := bin/$$(MODULE_NAME)
else ifeq ($$(MODULE_TYPE),shared_library)
$(1)_INSTALL := lib/lib$$(MODULE_NAME).so
else
$(1)_INSTALL := lib/lib$$(MODULE_NAME).a
endif

$$($(1)_OBJS): MODULE_INCS := $$($(1)_INCS)
$$($(1)_OBJS): MODULE_EXTRA := $$($(1)_CFLAGS)

$(BUILD)/$(1)/%.o: %.c
	@mkdir -p $$(dir $$@)
	$$(CC) $$(CFLAGS) $$(MODULE_INCS) $$(MODULE_EXTRA) $$(EXTRA_CFLAGS) -c -o $$@ $$<

$(BUILD)/$(1)/%.o: %.cpp
	@mkdir -p $$(dir $$@)
	$$(CXX) $$(CXXFLAGS) $$(MODULE_INCS) $$(MODULE_EXTRA) $$(EXTRA_CXXFLAGS) -c -o $$@ $$<

# ddk_driver_sources() accepts .cc and .S too; a missing rule here shows up as
# "No rule to make target ...o", which is a confusing way to learn that.
$(BUILD)/$(1)/%.o: %.cc
	@mkdir -p $$(dir $$@)
	$$(CXX) $$(CXXFLAGS) $$(MODULE_INCS) $$(MODULE_EXTRA) $$(EXTRA_CXXFLAGS) -c -o $$@ $$<

$(BUILD)/$(1)/%.o: %.S
	@mkdir -p $$(dir $$@)
	$$(CC) $$(CFLAGS) $$(MODULE_INCS) $$(MODULE_EXTRA) $$(EXTRA_CFLAGS) -c -o $$@ $$<
endef

$(foreach m,$(MODULES),$(eval $(call module_rules,$(m))))

# Link rules run after every module is known, so that a module can depend on
# a library another module builds.
#
# A link needs the whole reachable set, not just the direct dependencies: the
# graph is already declared, one edge per BUILD.gn, so it is computed here
# rather than restated by hand in every executable.  Restating it is how the
# two descriptions drift.
dep_round = $(sort $(1) $(foreach m,$(1),$($(m)_DEPS)))
dep_closure = $(call dep_round,$(call dep_round,$(call dep_round,\
                $(call dep_round,$(1)))))
# The link flags are chosen here, while the rule is written, not left to be
# looked up when it runs: a recipe is expanded at execution time, when a
# variable set per module holds whatever the last module left in it.
define link_rules
$(1)_ALLDEPS := $$(call dep_closure,$$($(1)_DEPS))

# A dependency naming a module that does not exist used to be dropped in
# silence, and the link then failed somewhere else or, for a shared object,
# not at all.
ifneq ($$(filter-out $(MODULES),$$($(1)_ALLDEPS)),)
$$(error $(1) depends on unknown module(s): \
  $$(filter-out $(MODULES),$$($(1)_ALLDEPS)))
endif
$(1)_ALLLIBS := $$(sort $$($(1)_LIBS) \
                        $$(foreach d,$$($(1)_ALLDEPS),$$($$(d)_LIBS)))
$(1)_DEPLIBS := $$(foreach d,$$($(1)_ALLDEPS),$$(MODULE_OUTPUT_$$(d)))

# Four rounds reach a depth of sixteen.  A graph deeper than that would link
# against an incomplete set, so say so rather than let it fail as a pile of
# undefined symbols.
ifneq ($$(call dep_round,$$($(1)_ALLDEPS)),$$($(1)_ALLDEPS))
$$(error dependency graph of $(1) is deeper than the closure computes)
endif

ifeq ($($(1)_TYPE),static_library)
$$($(1)_OUT): $$($(1)_OBJS)
	@mkdir -p $$(dir $$@)
	$(TOOLCHAIN)/bin/llvm-ar rcs $$@ $$^
else
$$($(1)_OUT): $$($(1)_OBJS) $$($(1)_DEPLIBS)
	@mkdir -p $$(dir $$@)
	$$($(1)_LD) $(LINK_$($(1)_TYPE)) $$(EXTRA_LDFLAGS) -o $$@ $$($(1)_OBJS) \
	    -Wl,--start-group \
	    $$(addprefix -l,$$($(1)_ALLDEPS)) $$(addprefix -l,$$($(1)_ALLLIBS)) \
	    -Wl,--end-group \
	    -Wl,--trace > $(BUILD)/$(1)/link.trace
endif
endef

LINK_driver         := $(LDFLAGS) $(STRIP_FLAGS)
LINK_shared_library := $(LDFLAGS) $(STRIP_FLAGS)
LINK_executable     := $(ARCH_FLAGS) --sysroot=$(SYSROOT) -L$(LIBDDK)/lib $(EXTRA_LIBS) -L$(LIBDIR) $(STRIP_FLAGS)

$(foreach m,$(MODULES),$(eval $(call link_rules,$(m))))

# `make mtk_ddk` from inside mtk_ddk builds it, the same as a bare `make`.
# Kept because that is the spelling the delivery was built with while this
# file sat above the halves, and because a half named as a target reads
# better in somebody's recipe than a bare `all`.
.PHONY: $(HALF)
$(HALF): all

# Declared last on purpose: make expands an explicit rule's prerequisites
# immediately, and ALL_OUTPUTS is filled in by the loops above.
all: $(ALL_OUTPUTS)

-include $(shell find $(BUILD) -name '*.d' 2>/dev/null)

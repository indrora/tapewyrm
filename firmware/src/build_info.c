/*
 * build_info.c
 * 
 * Written & released by Keir Fraser <keir.xen@gmail.com>
 * 
 * This is free and unencumbered software released into the public domain.
 * See the file COPYING for more details, or visit <http://unlicense.org>.
 */

const uint8_t fw_major = FW_MAJOR;
const uint8_t fw_minor = FW_MINOR;

/* Tapewyrm: the git commit this image was built from (40 hex chars, or "" if
 * git was unavailable) and whether firmware/ or protocol/ had uncommitted
 * changes. tw_git.h is generated into the build dir by scripts/pio_pre.py and
 * reported to the host by the BUILD_INFO verb (qic/qic.c). */
#include "tw_git.h"
const char tw_git_commit[41] = TW_GIT_COMMIT;
const uint8_t tw_git_dirty = TW_GIT_DIRTY;

/*
 * Local variables:
 * mode: C
 * c-file-style: "Linux"
 * c-basic-offset: 4
 * tab-width: 4
 * indent-tabs-mode: nil
 * End:
 */

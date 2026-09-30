/* Compile against real Linux UAPI and libdevmapper development headers. */
#include <stddef.h>
#include <stdio.h>
#include <sys/ioctl.h>
#include <linux/fs.h>
#include <linux/dm-ioctl.h>
#include <libdevmapper.h>

#if defined(__x86_64__)
#define MACHINE "x86_64"
#elif defined(__aarch64__)
#define MACHINE "aarch64"
#elif defined(__riscv) && __riscv_xlen == 64
#define MACHINE "riscv64"
#else
#error Unsupported probe architecture
#endif

int main(void)
{
    printf("{\"machine\":\"%s\",\"pointer_bytes\":%zu,"
           "\"size_t_bytes\":%zu,\"int_bytes\":%zu,\"byteorder\":\"%s\","
           "\"BLKGETSIZE64\":%lu,\"BLKSSZGET\":%lu,"
           "\"BLKGETDISKSEQ\":%lu,\"DM_MPATH_PROBE_PATHS\":%lu,"
           "\"dm_info_size\":%zu,\"dm_info_alignment\":%zu,"
           "\"dm_info_offsets\":{",
           MACHINE, sizeof(void *), sizeof(size_t), sizeof(int),
           __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__ ? "little" : "big",
           (unsigned long)BLKGETSIZE64, (unsigned long)BLKSSZGET,
           (unsigned long)BLKGETDISKSEQ, (unsigned long)DM_MPATH_PROBE_PATHS,
           sizeof(struct dm_info), _Alignof(struct dm_info));
#define FIELD(name, comma) \
    printf("\"" #name "\":%zu" comma, offsetof(struct dm_info, name))
    FIELD(exists, ",");
    FIELD(suspended, ",");
    FIELD(live_table, ",");
    FIELD(inactive_table, ",");
    FIELD(open_count, ",");
    FIELD(event_nr, ",");
    FIELD(major, ",");
    FIELD(minor, ",");
    FIELD(read_only, ",");
    FIELD(target_count, ",");
    FIELD(deferred_remove, ",");
    FIELD(internal_suspend, "");
    puts("}}");
    return 0;
}

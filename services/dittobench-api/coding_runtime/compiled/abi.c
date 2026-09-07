#include <linux/seccomp.h>
#include <stdio.h>
#include <sys/ioctl.h>

int main(void) {
    printf("%lu %lu %lu %zu %zu\n", (unsigned long)SECCOMP_IOCTL_NOTIF_RECV,
           (unsigned long)SECCOMP_IOCTL_NOTIF_SEND, (unsigned long)SECCOMP_IOCTL_NOTIF_ID_VALID,
           sizeof(struct seccomp_notif), sizeof(struct seccomp_notif_resp));
}

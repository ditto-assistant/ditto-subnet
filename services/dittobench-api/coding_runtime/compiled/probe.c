/* Public synthetic candidate: attacks execute from ELF constructors, not main. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>

static void need(int condition) { if (!condition) _exit(23); }
static int initialized;

static void *thread_check(void *unused) {
    (void)unused;
    errno = 0;
    need(syscall(SYS_fork) == -1 && errno == EPERM);
    need(syscall(SYS_tgkill, getpid(), syscall(SYS_gettid), 0) == 0);
    return NULL;
}

__attribute__((constructor)) static void initialize(void) {
    int marker = open("/tmp/compiled-candidate-started", O_WRONLY | O_CREAT | O_EXCL, 0600);
    need(marker >= 0 && close(marker) == 0);
    need(getuid() != 0 && geteuid() == getuid() && getgid() != 0 && getgroups(0, NULL) == 0);
    need(prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) == 1);
    need(prctl(PR_GET_SECCOMP, 0, 0, 0, 0) == SECCOMP_MODE_FILTER);
    errno = 0; need(syscall(SYS_fork) == -1 && errno == EPERM);
    errno = 0; need(setsid() == -1 && errno == EPERM);
    errno = 0; need(kill(getppid(), 0) == -1 && errno == EPERM);
    need(kill(getpid(), 0) == 0);
    char *args[] = {"true", NULL};
    char *env[] = {NULL};
    errno = 0; need(execve("/bin/true", args, env) == -1 && errno == EPERM);
    int executable = open("/bin/true", O_PATH | O_CLOEXEC);
    need(executable >= 0);
    errno = 0;
    need(syscall(SYS_execveat, executable, "", args, env, AT_EMPTY_PATH) == -1 && errno == ENOSYS);
    close(executable);
    struct sock_filter allow[] = {BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW)};
    struct sock_fprog program = {.len = 1, .filter = allow};
    errno = 0;
    need(syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER, 0, &program) == -1 && errno == EPERM);
    errno = 0;
    need(prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &program) == -1 && errno == EPERM);
    errno = 0;
    need(syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER, SECCOMP_FILTER_FLAG_NEW_LISTENER, &program) == -1 && errno == EPERM);
    errno = 0;
    need(open("/run/dittobench-grader/secret", O_RDONLY) == -1 && errno == EACCES);
    errno = 0;
    need(open("/run/dittobench-control/report", O_WRONLY | O_CREAT, 0600) == -1 && errno == EACCES);
    need(getenv("SYNTHETIC_SECRET") == NULL && getenv("LD_PRELOAD") == NULL);
    initialized = 1;
}

int main(void) {
    need(initialized);
    pthread_t thread;
    need(pthread_create(&thread, NULL, thread_check, NULL) == 0);
    need(pthread_join(thread, NULL) == 0);
    printf("compiled C constructors and thread isolation passed\n");
    return 0;
}

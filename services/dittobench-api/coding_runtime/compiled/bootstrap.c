/* Trusted, single-threaded pre-exec stub. Never load candidate code here.
 * The listener is used once, BEFORE candidate entry, then permanently closed.
 * No notification from untrusted code is ever granted or emulated.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <elf.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <limits.h>
#include <linux/audit.h>
#include <linux/capability.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#if !defined(__linux__) || !defined(__x86_64__) || defined(__ILP32__)
#error "compiled Coding bootstrap requires Linux amd64"
#endif

#define DENY(nr) \
    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (nr), 0, 1), \
    BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM)
#define SELF_SIGNAL(nr, self) \
    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (nr), 0, 4), \
    BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[0])), \
    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (self), 1, 0), \
    BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM), \
    BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW)

static void require(int condition) { if (!condition) _exit(70); }

static unsigned int number(const char *value) {
    require(value && value[0] >= '1' && value[0] <= '9');
    for (const char *p = value; *p; p++) require(*p >= '0' && *p <= '9');
    char *end;
    errno = 0;
    unsigned long parsed = strtoul(value, &end, 10);
    require(!errno && !*end && parsed < UINT_MAX);
    return (unsigned int)parsed;
}

static void single_thread(void) {
    DIR *directory = opendir("/proc/self/task");
    require(directory != NULL);
    unsigned int count = 0;
    struct dirent *entry;
    while ((entry = readdir(directory))) {
        if (entry->d_name[0] != '.') count++;
    }
    require(closedir(directory) == 0 && count == 1);
}

static void identity(unsigned int uid, unsigned int gid) {
    require(getuid() == 0 && geteuid() == 0);
    require(setgroups(0, NULL) == 0 && setresgid(gid, gid, gid) == 0 &&
            setresuid(uid, uid, uid) == 0);
    uid_t real_uid, effective_uid, saved_uid;
    gid_t real_gid, effective_gid, saved_gid;
    struct __user_cap_header_struct header = {
        .version = _LINUX_CAPABILITY_VERSION_3, .pid = 0,
    };
    struct __user_cap_data_struct caps[2] = {{0}};
    require(getresuid(&real_uid, &effective_uid, &saved_uid) == 0 &&
            getresgid(&real_gid, &effective_gid, &saved_gid) == 0 &&
            real_uid == uid && effective_uid == uid && saved_uid == uid &&
            real_gid == gid && effective_gid == gid && saved_gid == gid &&
            getgroups(0, NULL) == 0 && syscall(SYS_capget, &header, caps) == 0);
    for (unsigned int i = 0; i < 2; i++)
        require(!caps[i].effective && !caps[i].permitted && !caps[i].inheritable);
    /* Prevent same-UID ptrace/process-memory mutation of the trusted stub while
     * the initial exec is suspended. No candidate handlers or threads exist. */
    require(prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0 &&
            prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == 0);
}

static int filter(void) {
    const unsigned int self = (unsigned int)getpid();
    struct sock_filter instructions[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_X86_64, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, 0x40000000, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_clone3, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | ENOSYS),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_clone, 0, 4),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[0])),
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, 0x10000 /* CLONE_THREAD */, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        /* Go's asynchronous preemption and libc thread signals stay within
         * this thread group; no process-group or foreign PID signals pass. */
        SELF_SIGNAL(SYS_kill, self),
        SELF_SIGNAL(SYS_tgkill, self),
        SELF_SIGNAL(SYS_rt_tgsigqueueinfo, self),
        DENY(SYS_fork), DENY(SYS_vfork), DENY(SYS_execve),
        DENY(SYS_ptrace), DENY(SYS_process_vm_readv), DENY(SYS_process_vm_writev),
        DENY(SYS_tkill), DENY(SYS_pidfd_send_signal), DENY(SYS_pidfd_getfd),
        DENY(SYS_setpgid), DENY(SYS_setsid), DENY(SYS_unshare), DENY(SYS_setns),
        DENY(SYS_io_uring_setup), DENY(SYS_io_uring_enter), DENY(SYS_io_uring_register),
        /* New filters could redirect USER_NOTIF to an attacker-owned listener.
         * Block BOTH installation APIs permanently before granting first exec. */
        DENY(SYS_seccomp),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_prctl, 0, 8),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[0])),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, PR_SET_SECCOMP, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, PR_SET_DUMPABLE, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, PR_SET_PTRACER, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_execveat, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_USER_NOTIF),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog program = {
        .len = (unsigned short)(sizeof(instructions) / sizeof(instructions[0])),
        .filter = instructions,
    };
    /* NEW_LISTENER is not combined with TSYNC: we proved this trusted process
     * has exactly one thread, and candidate-created threads inherit the filter. */
    int listener = (int)syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER,
                                SECCOMP_FILTER_FLAG_NEW_LISTENER, &program);
    require(listener >= 0);
    return listener;
}

int main(int argc, char **argv) {
    require(argc == 5);
    unsigned int binary = number(argv[1]), notify = number(argv[2]);
    unsigned int uid = number(argv[3]), gid = number(argv[4]);
    require(binary >= 3 && binary < 1024 && notify >= 3 && notify < 1024 && binary != notify);
    struct stat info;
    require(fstat((int)binary, &info) == 0 && S_ISREG(info.st_mode) &&
            (info.st_mode & 07777) == 0555 && info.st_uid == 0 &&
            info.st_size >= (off_t)sizeof(Elf64_Ehdr) && info.st_size <= (256LL << 20));
    if (info.st_nlink == 0) {
        /* Anonymous executables need immutable byte/size/seal state. This is
         * not acceptance of arbitrary unlinked files or writable memfds. */
        const int required_seals = F_SEAL_SEAL | F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE;
        int seals = fcntl((int)binary, F_GET_SEALS);
        require(seals >= 0 && (seals & required_seals) == required_seals);
    } else {
        require(info.st_nlink == 1);
    }
    Elf64_Ehdr elf;
    require(pread((int)binary, &elf, sizeof(elf), 0) == sizeof(elf) &&
            !memcmp(elf.e_ident, ELFMAG, SELFMAG) && elf.e_ident[EI_CLASS] == ELFCLASS64 &&
            elf.e_ident[EI_DATA] == ELFDATA2LSB && elf.e_machine == EM_X86_64 &&
            (elf.e_type == ET_EXEC || elf.e_type == ET_DYN));
    int type, domain;
    socklen_t length = sizeof(type);
    require(getsockopt((int)notify, SOL_SOCKET, SO_TYPE, &type, &length) == 0 && type == SOCK_SEQPACKET);
    length = sizeof(domain);
    require(getsockopt((int)notify, SOL_SOCKET, SO_DOMAIN, &domain, &length) == 0 && domain == AF_UNIX);
    struct ucred peer;
    length = sizeof(peer);
    require(getsockopt((int)notify, SOL_SOCKET, SO_PEERCRED, &peer, &length) == 0 &&
            peer.uid == 0 && peer.pid == getppid());
    require(fcntl((int)binary, F_SETFD, FD_CLOEXEC) == 0);
    single_thread();
    identity(uid, gid);
    int listener = filter();
    uint32_t handoff[5] = {1, (uint32_t)getpid(), binary, uid, gid};
    union { struct cmsghdr align; char bytes[CMSG_SPACE(sizeof(int))]; } ancillary = {0};
    struct iovec data = {.iov_base = handoff, .iov_len = sizeof(handoff)};
    struct msghdr message = {.msg_iov = &data, .msg_iovlen = 1,
                            .msg_control = ancillary.bytes, .msg_controllen = sizeof(ancillary.bytes)};
    struct cmsghdr *rights = CMSG_FIRSTHDR(&message);
    rights->cmsg_level = SOL_SOCKET;
    rights->cmsg_type = SCM_RIGHTS;
    rights->cmsg_len = CMSG_LEN(sizeof(int));
    memcpy(CMSG_DATA(rights), &listener, sizeof(listener));
    require(sendmsg((int)notify, &message, MSG_NOSIGNAL) == sizeof(handoff));
    require(close(listener) == 0 && close((int)notify) == 0);
    char *candidate_argv[] = {"candidate", NULL};
    char *candidate_env[] = {"PATH=/usr/local/bin:/usr/bin:/bin", NULL};
    syscall(SYS_execveat, (int)binary, "", candidate_argv, candidate_env, AT_EMPTY_PATH);
    /* Failed initial execution never falls back to another image/path. */
    _exit(70);
}

/* Linux amd64 N-API confinement, installed before any candidate import.
 * TSYNC is mandatory: Node/V8 have existing threads before this addon loads.
 * This is one layer inside the isolated rootless executor, not a host sandbox.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <linux/audit.h>
#include <linux/capability.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <node_api.h>
#include <stddef.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>

#if !defined(__linux__) || !defined(__x86_64__) || defined(__ILP32__)
#error "Native Coding Node confinement requires Linux amd64"
#endif

#define DENY(nr) \
    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, (nr), 0, 1), \
    BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM)

static napi_value confine(napi_env env, napi_callback_info info) {
    (void)info;
    uid_t real_uid, effective_uid, saved_uid;
    gid_t real_gid, effective_gid, saved_gid;
    struct __user_cap_header_struct cap_header = {
        .version = _LINUX_CAPABILITY_VERSION_3, .pid = 0,
    };
    struct __user_cap_data_struct caps[2] = {{0}};
    /* The trusted parent sets uid/gid and clears supplementary groups. */
    if (getresuid(&real_uid, &effective_uid, &saved_uid) != 0 ||
        getresgid(&real_gid, &effective_gid, &saved_gid) != 0 ||
        real_uid == 0 || real_gid == 0 || real_uid != effective_uid ||
        real_uid != saved_uid || real_gid != effective_gid || real_gid != saved_gid ||
        getgroups(0, NULL) != 0 || syscall(SYS_capget, &cap_header, caps) != 0 ||
        caps[0].effective || caps[0].permitted || caps[0].inheritable ||
        caps[1].effective || caps[1].permitted || caps[1].inheritable) {
        napi_throw_error(env, NULL, "invalid candidate identity");
        return NULL;
    }
    struct sock_filter filter[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH_X86_64, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, 0x40000000, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        /* clone3 cannot inspect pointed-to flags. Force libc's clone fallback;
         * only same-thread-group clones may survive the parent's kill/reap. */
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_clone3, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | ENOSYS),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_clone, 0, 4),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[0])),
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, 0x10000 /* CLONE_THREAD */, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        DENY(SYS_fork), DENY(SYS_vfork), DENY(SYS_execve), DENY(SYS_execveat),
        DENY(SYS_ptrace), DENY(SYS_process_vm_readv), DENY(SYS_process_vm_writev),
        DENY(SYS_kill), DENY(SYS_tkill), DENY(SYS_tgkill),
        DENY(SYS_pidfd_send_signal), DENY(SYS_pidfd_getfd),
        DENY(SYS_setpgid), DENY(SYS_setsid), DENY(SYS_unshare), DENY(SYS_setns),
        DENY(SYS_io_uring_setup), DENY(SYS_io_uring_enter), DENY(SYS_io_uring_register),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog program = {
        .len = (unsigned short)(sizeof(filter) / sizeof(filter[0])),
        .filter = filter,
    };
    /* Positive seccomp returns also mean TSYNC failure. Never fall back to
     * prctl(PR_SET_SECCOMP), which would leave existing V8 threads unfiltered. */
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 ||
        syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER,
                SECCOMP_FILTER_FLAG_TSYNC, &program) != 0) {
        napi_throw_error(env, NULL, "candidate thread confinement unavailable");
        return NULL;
    }
    napi_value result;
    if (napi_get_boolean(env, 1, &result) != napi_ok) {
        napi_throw_error(env, NULL, "confinement result unavailable");
        return NULL;
    }
    return result;
}

static napi_value initialize(napi_env env, napi_value exports) {
    napi_value function;
    if (napi_create_function(env, "confine", NAPI_AUTO_LENGTH, confine, NULL,
                             &function) != napi_ok ||
        napi_set_named_property(env, exports, "confine", function) != napi_ok) {
        napi_throw_error(env, NULL, "confinement initialization unavailable");
        return NULL;
    }
    return exports;
}

NAPI_MODULE(NODE_GYP_MODULE_NAME, initialize)

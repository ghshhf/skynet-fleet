// 跑在 Shizuku 提供的 shell 身份（uid 2000）里的最小执行面。
//
// 13.x 的 Shizuku 已经没有 newProcess；要做跨 App 特权只能自带 UserService：
// 这个接口由 App 定义、由 Shizuku 在它自己的进程里实例化（见 ShellService），
// App 通过 bindUserService 拿到代理。只暴露一条 exec，不做文件传输。
package app.skynet.tianwang.next;

interface IUserService {
    /** 在 shell 身份下执行 `sh -c cmd`；stdout/stderr 合并返回，输出有上限。 */
    String exec(String cmd, String cwd, int timeoutMs);
}

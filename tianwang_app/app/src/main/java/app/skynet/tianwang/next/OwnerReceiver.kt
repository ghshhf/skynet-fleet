package app.skynet.tianwang.next

import android.app.admin.DeviceAdminReceiver

/**
 * 设备所有者 / 设备管理接收器。
 *
 * 只作为 [android.app.admin.DevicePolicyManager] 的身份锚点，不做 UI、不弹提示。
 * 全新手机可用一条 adb 命令（见 `self.owner.provision`）把本 App 设为设备所有者，
 * 无需 root、无需解锁 Bootloader —— 这是类比 Shizuku 的第二层「假 root」。
 */
class OwnerReceiver : DeviceAdminReceiver()

package care.ira.watch.devicepolicy

import android.app.admin.DevicePolicyManager
import android.content.ComponentName
import android.content.Context

/**
 * The thin wrapper from [DevicePolicy]'s decisions to the platform (aosp flavour only; IRA CARE
 * OS). Every call needs the caller to be the device owner, which only provisioning grants (E4.f,
 * needs an image); until then this class is compiled but no component registers it, and the
 * watch app is not a device admin. Nothing here has run on a device.
 */
class DevicePolicyManagerTarget(context: Context, private val admin: ComponentName) : PolicyTarget {
    private val dpm = context.getSystemService(DevicePolicyManager::class.java)

    override fun addRestriction(key: String) = dpm.addUserRestriction(admin, key)

    override fun clearRestriction(key: String) = dpm.clearUserRestriction(admin, key)

    override fun setKeyguardDisabled(disabled: Boolean) {
        dpm.setKeyguardDisabled(admin, disabled)
    }

    @Suppress("DEPRECATION")
    override fun wipe(reason: String) = dpm.wipeData(0)
}

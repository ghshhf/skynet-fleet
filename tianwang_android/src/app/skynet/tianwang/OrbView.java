package app.skynet.tianwang;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.RadialGradient;
import android.graphics.Shader;
import android.os.SystemClock;
import android.util.AttributeSet;
import android.view.View;

/**
 * 光球。
 *
 * 不引第三方动画库，全部用 Canvas 手绘：一层外发光 + 三层呼吸环 + 一个实心核。
 * 四种状态各有自己的颜色和节奏，靠颜色一眼分辨"在听"还是"在说"，
 * 靠环的扩散速度分辨"在想"（想的时候环往内收，听起来更专注）。
 *
 * 只在可见时刷新（onVisibilityChanged 控制），待机降到 20fps，
 * 免得为了一个球白白烤电池。
 */
public class OrbView extends View {

    public static final int IDLE = 0;
    public static final int LISTEN = 1;
    public static final int THINK = 2;
    public static final int SPEAK = 3;

    // 配色统一走 values/colors.xml（含 values-night 的深色版），
    // 这里不再写死 ARGB —— 写死就会在另一套主题下变成看不见或太黑。

    private final Paint corePaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint ringPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint glowPaint = new Paint(Paint.ANTI_ALIAS_FLAG);

    private int state = IDLE;
    private int level;                 // 0..100，录音时的输入音量，让球跟着声音抖
    private float phase;               // 0..1 循环，驱动所有动画
    private long lastTick;
    private boolean visible = true;

    public OrbView(Context c, AttributeSet a) {
        super(c, a);
        ringPaint.setStyle(Paint.Style.STROKE);
        glowPaint.setStyle(Paint.Style.FILL);
        corePaint.setStyle(Paint.Style.FILL);
        lastTick = SystemClock.uptimeMillis();
    }

    public void setState(int s) {
        if (state == s) { return; }
        state = s;
        level = 0;
        invalidate();
    }

    public int getState() {
        return state;
    }

    /** 录音回调里直接调；别在 UI 线程做重活，这里只存一个数。 */
    public void setLevel(int lv) {
        level = lv < 0 ? 0 : (lv > 100 ? 100 : lv);
    }

    @Override
    protected void onVisibilityChanged(View changedView, int visibility) {
        super.onVisibilityChanged(changedView, visibility);
        visible = visibility == VISIBLE;
        if (visible) {
            lastTick = SystemClock.uptimeMillis();
            postInvalidateOnAnimationCompat();
        }
    }

    @Override
    protected void onDetachedFromWindow() {
        visible = false;
        super.onDetachedFromWindow();
    }

    private int color() {
        // 颜色必须从资源取，不能写死：这个 App 有浅色和深色两套底，
        // 原来待机色是写死的深灰 —— 换到浅色底上，球就成了一个黑洞。
        int res;
        switch (state) {
            case LISTEN: res = R.color.listen; break;
            case THINK:  res = R.color.think;  break;
            case SPEAK:  res = R.color.accent; break;
            default:     res = R.color.orb_idle;
        }
        return getResources().getColor(res, null);
    }

    /** 速度：待机最慢，听/说中等，想最快。 */
    private float speed() {
        switch (state) {
            case LISTEN: return 0.45f;
            case THINK:  return 1.15f;
            case SPEAK:  return 0.75f;
            default:     return 0.18f;
        }
    }

    private void postInvalidateOnAnimationCompat() {
        // API 16+ 就有，但这里显式走 postInvalidateDelayed 保证旧机上节奏一致
        postInvalidateDelayed(state == IDLE ? 50 : 33);
    }

    @Override
    protected void onDraw(Canvas cv) {
        long now = SystemClock.uptimeMillis();
        float dt = Math.min(0.1f, (now - lastTick) / 1000f);
        lastTick = now;
        if (visible) {
            phase += dt * speed();
            if (phase >= 1f) { phase -= 1f; }
        }

        int w = getWidth(), h = getHeight();
        float cx = w / 2f, cy = h / 2f;
        float base = Math.min(w, h) * 0.26f;          // 核的基准半径
        int c = color();

        // 听的时候随音量鼓一下，最多 +18%
        float pulse = 0f;
        if (state == LISTEN) {
            pulse = base * 0.18f * (level / 100f);
        } else if (state == SPEAK) {
            pulse = base * 0.10f * (float) Math.sin(phase * Math.PI * 6);
        } else if (state == THINK) {
            pulse = base * 0.05f * (float) Math.sin(phase * Math.PI * 10);
        }
        float coreR = base + pulse;

        // ① 外发光：径向渐变，从核的颜色淡出到全透明
        glowPaint.setShader(new RadialGradient(cx, cy, coreR * 3.1f,
                new int[]{withAlpha(c, 90), withAlpha(c, 26), Color.TRANSPARENT},
                new float[]{0f, 0.45f, 1f}, Shader.TileMode.CLAMP));
        cv.drawCircle(cx, cy, coreR * 3.1f, glowPaint);
        glowPaint.setShader(null);

        // ② 三层环：同一相位错开 0.33，形成连续外扩的涟漪
        ringPaint.setColor(withAlpha(c, 130));
        ringPaint.setStrokeWidth(Math.max(1.5f, base * 0.035f));
        for (int i = 0; i < 3; i++) {
            float p = phase + i / 3f;
            if (p >= 1f) { p -= 1f; }
            float r, a;
            if (state == THINK) {
                // 想：从外往内收，收到核上消失
                r = coreR * (1.9f - 0.9f * p);
                a = (1f - p) * 130f;
            } else {
                r = coreR * (1.0f + 1.35f * p);
                a = (1f - p) * 110f;
            }
            ringPaint.setColor(withAlpha(c, (int) a));
            cv.drawCircle(cx, cy, r, ringPaint);
        }

        // ③ 核：中心实心 + 一层内亮边，显得有体积
        corePaint.setColor(c);
        corePaint.setAlpha(235);
        cv.drawCircle(cx, cy, coreR, corePaint);
        corePaint.setColor(Color.WHITE);
        corePaint.setAlpha(state == IDLE ? 26 : 60);
        cv.drawCircle(cx - coreR * 0.28f, cy - coreR * 0.3f, coreR * 0.42f, corePaint);

        if (visible) {
            postInvalidateOnAnimationCompat();
        }
    }

    private static int withAlpha(int color, int alpha) {
        int a = alpha < 0 ? 0 : (alpha > 255 ? 255 : alpha);
        return (color & 0x00FFFFFF) | (a << 24);
    }
}

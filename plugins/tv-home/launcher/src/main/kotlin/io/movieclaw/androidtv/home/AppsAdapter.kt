package io.movieclaw.androidtv.home

import android.content.Context
import android.util.AttributeSet
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.TextView
import androidx.recyclerview.widget.GridLayoutManager
import androidx.recyclerview.widget.RecyclerView

private const val REGULAR = 0
private const val FEATURED = 1

/** 「全部应用」的格子（§6.3）：MovieClaw 占一整行、卡片更大；焦点在的那格放大 */
class AppsAdapter(private val onClick: (App) -> Unit) : RecyclerView.Adapter<AppsAdapter.Holder>() {
    private var apps = emptyList<App>()

    init {
        // 稳定 id：整表刷新后 RecyclerView 把焦点还给原来那格
        setHasStableIds(true)
    }

    @Suppress("NotifyDataSetChanged")
    fun submit(list: List<App>) {
        apps = list
        notifyDataSetChanged()
    }

    fun spans(columns: Int) = object : GridLayoutManager.SpanSizeLookup() {
        override fun getSpanSize(position: Int) = if (apps[position].featured) columns else 1
    }

    override fun getItemCount() = apps.size

    override fun getItemId(position: Int) = apps[position].id

    override fun getItemViewType(position: Int) = if (apps[position].featured) FEATURED else REGULAR

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int) = Holder(
        LayoutInflater.from(parent.context).inflate(if (viewType == FEATURED) R.layout.item_featured else R.layout.item_app, parent, false),
    )

    override fun onBindViewHolder(holder: Holder, position: Int) = holder.bind(apps[position])

    inner class Holder(view: View) : RecyclerView.ViewHolder(view) {
        private val card: View = view.findViewById(R.id.card)
        private val image: ImageView = view.findViewById(R.id.image)
        private val label: TextView = view.findViewById(R.id.label)
        private val inset = view.resources.getDimensionPixelSize(R.dimen.icon_inset)

        init {
            card.clipToOutline = true
            view.setOnClickListener { apps.getOrNull(bindingAdapterPosition)?.let(onClick) }
            view.setOnFocusChangeListener { _, focused ->
                val scale = if (focused) 1.08f else 1f
                card.animate().scaleX(scale).scaleY(scale).setDuration(150).start()
            }
        }

        fun bind(app: App) {
            image.setImageDrawable(app.image)
            // 没有横幅的（多是手机应用）：图标居中，四周留边
            image.scaleType = if (app.banner) ImageView.ScaleType.CENTER_CROP else ImageView.ScaleType.FIT_CENTER
            val pad = if (app.banner) 0 else inset
            image.setPadding(pad, pad, pad, pad)
            label.text = app.label
        }
    }
}

/** 16:9 的卡片（电视横幅的比例）：高度跟着宽度走 */
class BannerFrame(context: Context, attrs: AttributeSet?) : FrameLayout(context, attrs) {
    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val width = MeasureSpec.getSize(widthMeasureSpec)
        super.onMeasure(widthMeasureSpec, MeasureSpec.makeMeasureSpec(width * 9 / 16, MeasureSpec.EXACTLY))
    }
}

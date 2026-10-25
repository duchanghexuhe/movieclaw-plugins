package app.orchard.tv.ui.components

import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.ExitTransition
import androidx.compose.animation.core.Easing
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.togetherWith
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier

/**
 * The new content fades in over the old, which stays whole beneath it until it is covered.
 *
 * A Crossfade fades both at once: two opaque pictures at half each cover only three quarters of
 * the screen mid-way, and the black behind them shows through as a dip. Only for content that is
 * opaque and covers the same area, or the old would vanish in one frame at the end.
 */
@Composable
fun <T> Dissolve(
    targetState: T,
    durationMs: Int,
    easing: Easing,
    modifier: Modifier = Modifier,
    label: String = "dissolve",
    content: @Composable (T) -> Unit,
) {
    AnimatedContent(
        targetState = targetState,
        modifier = modifier,
        transitionSpec = {
            (fadeIn(tween(durationMs, easing = easing)) togetherWith ExitTransition.KeepUntilTransitionsFinished)
                .using(null)
        },
        label = label,
    ) { content(it) }
}

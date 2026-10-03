package uk.nktnet.webviewkiosk.ui.components.auth

import androidx.activity.compose.LocalActivity
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import uk.nktnet.webviewkiosk.R
import uk.nktnet.webviewkiosk.config.Constants
import uk.nktnet.webviewkiosk.states.LockStateSingleton
import uk.nktnet.webviewkiosk.utils.tryUnlockTask

@Composable
fun QuonixLockScreen() {
    val activity = LocalActivity.current
    val isLocked by LockStateSingleton.isLocked
    var password by remember { mutableStateOf("") }

    if (!isLocked) {
        return
    }

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black),
        contentAlignment = Alignment.Center
    ) {
        Image(
            painter = painterResource(R.drawable.quonix_lock),
            contentDescription = "QUONIX kiosk lock screen",
            modifier = Modifier.fillMaxSize(),
            contentScale = ContentScale.Crop,
        )

        OutlinedTextField(
            value = password,
            onValueChange = { password = it },
            label = { Text("Passwort") },
            placeholder = { Text("Kiosk entsperren") },
            singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
            keyboardActions = KeyboardActions(
                onDone = {
                    if (password == Constants.KIOSK_UNLOCK_PASSWORD) {
                        password = ""
                        tryUnlockTask(activity)
                    }
                }
            ),
            modifier = Modifier
                .fillMaxWidth(0.62f)
                .align(Alignment.Center)
                .padding(top = 190.dp),
            textStyle = androidx.compose.ui.text.TextStyle(
                fontSize = 18.sp,
                color = Color.White,
            ),
        )
    }
}

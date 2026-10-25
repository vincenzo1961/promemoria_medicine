[app]
title = Promemoria Medicine
package.name = promemoriamedicine
package.domain = org.vincenzo
source.dir = .
source.include_exts = py,png,jpg,wav
version = 1.6

icon.filename = %(source.dir)s/icon_512.png
# icona adattiva: disegno e sfondo separati, così Samsung non la rimpicciolisce nel riquadro bianco
icon.adaptive_foreground.filename = %(source.dir)s/icon_fg.png
icon.adaptive_background.filename = %(source.dir)s/icon_bg.png
presplash.filename = %(source.dir)s/presplash.png
android.presplash_color = #FFF8E7

requirements = python3,kivy,pyjnius,android

# l'app può fare da schermata Home: così parte da sola all'accensione del telefono
android.manifest.intent_filters = intent_filters.xml

orientation = portrait
fullscreen = 0

android.permissions = INTERNET,READ_EXTERNAL_STORAGE,READ_MEDIA_IMAGES,READ_MEDIA_AUDIO,WAKE_LOCK,SEND_SMS,SCHEDULE_EXACT_ALARM,USE_EXACT_ALARM
android.api = 33
android.minapi = 21
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1

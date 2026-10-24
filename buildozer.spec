[app]
title = Promemoria Medicine
package.name = promemoriamedicine
package.domain = org.vincenzo
source.dir = .
source.include_exts = py,png,jpg,wav
version = 1.1

icon.filename = %(source.dir)s/icon_512.png
presplash.filename = %(source.dir)s/presplash.png
android.presplash_color = #FFF8E7

requirements = python3,kivy,pyjnius,android

orientation = portrait
fullscreen = 0

android.permissions = READ_EXTERNAL_STORAGE,READ_MEDIA_IMAGES,READ_MEDIA_AUDIO,WAKE_LOCK
android.api = 33
android.minapi = 21
android.archs = arm64-v8a, armeabi-v7a
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1

# -*- coding: utf-8 -*-
"""
PROMEMORIA MEDICINE - versione Android (Kivy)
Pensata per un telefono/tablet dedicato, sempre in carica e con l'app aperta.

- Orologio grande + elenco delle medicine di oggi
- All'ora giusta: schermata a tutto schermo con foto, nome, dose,
  suoneria della sveglia (volume crescente, si sente anche in silenzioso)
  alternata alla voce (sintesi vocale di Android o messaggio registrato)
- Pulsanti: L'HO PRESA / RICORDAMELO TRA 10 MINUTI / RIPETI
- Impostazioni protette: si aprono tenendo premuto il pulsante 2 secondi
- Storico delle risposte

Prova su PC:  pip install kivy pyttsx3   poi   python main.py
"""

import csv
import datetime as dt
import json
import math
import os
import shutil
import struct
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave

from kivy.app import App
from kivy.clock import Clock
from kivy.core.audio import SoundLoader
from kivy.core.window import Window
from kivy.graphics import Color, Rectangle
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.image import Image
from kivy.uix.label import Label
from kivy.uix.modalview import ModalView
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import NoTransition, Screen, ScreenManager
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.utils import get_color_from_hex as hexc
from kivy.utils import platform

ANDROID = platform == "android"

# ---------------- impostazioni ----------------
RIMANDA_MIN = 10        # minuti del pulsante "ricordamelo dopo"
PRIMO_SQUILLO_SEC = 8   # quanto suona la suoneria prima della prima voce
SQUILLO_SEC = 15        # poi: quanto suona la suoneria tra un annuncio e l'altro
VOLUME_INIZIALE = 0.15  # la suoneria parte piano...
CRESCENDO_SEC = 25      # ...e arriva al massimo in questi secondi
VOLUME_MINIMO = 0.6     # se il volume SVEGLIA del telefono è sotto il 60%, lo alza a 60%
TIMEOUT_MIN = 30        # dopo quanto, senza risposta, segna "non confermata"
MAX_RITARDO_ORE = 2     # dopo un riavvio non suona per orari più vecchi di così

BG = "#FFF8E7"
BG_ALLARME = "#FFE08A"
VERDE = "#2E9E44"
ARANCIO = "#E8822A"
BLU = "#2C6FB7"
ROSSO = "#B00020"
GRIGIO = "#777777"
TESTO = "#222222"

# Colori disponibili per le persone: (forte, chiaro per le righe, sfondo della sveglia)
PALETTE = {
    "rosa":    ("#D6457A", "#FCE4EE", "#F9C9DC"),
    "azzurro": ("#2C6FB7", "#E1EEFB", "#BFDCF7"),
    "verde":   ("#2E9E44", "#E3F4E6", "#C5EBCB"),
    "viola":   ("#7B4FB8", "#EEE6F8", "#DDCCF2"),
    "arancio": ("#E07020", "#FDEEDF", "#FAD7B5"),
    "giallo":  ("#B38F00", "#FFF6CC", "#FFE88A"),
}

GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
        "agosto", "settembre", "ottobre", "novembre", "dicembre"]

EST_FOTO = (".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp")
EST_AUDIO = (".mp3", ".m4a", ".wav", ".ogg", ".aac", ".amr", ".3gp")


# =====================================================
#  FUNZIONI SPECIFICHE ANDROID
# =====================================================
if ANDROID:
    from jnius import autoclass
    from android.runnable import run_on_ui_thread

    PythonActivity = autoclass("org.kivy.android.PythonActivity")

    @run_on_ui_thread
    def imposta_schermo(sempre_acceso):
        """Mostra l'app anche sopra il blocco schermo e decide se lo schermo resta acceso."""
        att = PythonActivity.mActivity
        finestra = att.getWindow()
        try:
            if autoclass("android.os.Build$VERSION").SDK_INT >= 27:
                att.setShowWhenLocked(True)
                att.setTurnScreenOn(True)
            else:
                # SHOW_WHEN_LOCKED | TURN_SCREEN_ON | DISMISS_KEYGUARD
                finestra.addFlags(0x00080000 | 0x00200000 | 0x00400000)
        except Exception as e:
            print("Blocco schermo:", e)
        if sempre_acceso:
            finestra.addFlags(128)          # FLAG_KEEP_SCREEN_ON
        else:
            finestra.clearFlags(128)

    def puo_aprirsi_da_sola():
        """Permesso «Mostra sopra altre app»: garantisce che la sveglia apra l'app anche
        quando è chiusa, su tutte le versioni di Android."""
        try:
            if autoclass("android.os.Build$VERSION").SDK_INT < 23:
                return True
            return bool(autoclass("android.provider.Settings").canDrawOverlays(PythonActivity.mActivity))
        except Exception:
            return True

    def chiedi_apertura_da_sola():
        try:
            Intent = autoclass("android.content.Intent")
            Uri = autoclass("android.net.Uri")
            att = PythonActivity.mActivity
            i = Intent("android.settings.action.MANAGE_OVERLAY_PERMISSION",
                       Uri.parse("package:" + att.getPackageName()))
            i.addFlags(0x10000000)
            att.startActivity(i)
        except Exception as e:
            print("Permesso sovrapposizione:", e)

    def batteria_senza_limiti():
        try:
            from jnius import cast
            att = PythonActivity.mActivity
            pm = cast("android.os.PowerManager", att.getSystemService("power"))
            return bool(pm.isIgnoringBatteryOptimizations(att.getPackageName()))
        except Exception:
            return True

    def chiedi_batteria_senza_limiti():
        try:
            Intent = autoclass("android.content.Intent")
            Uri = autoclass("android.net.Uri")
            att = PythonActivity.mActivity
            i = Intent("android.settings.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS",
                       Uri.parse("package:" + att.getPackageName()))
            i.addFlags(0x10000000)
            att.startActivity(i)
        except Exception as e:
            print("Batteria:", e)
            apri_impostazioni_telefono("android.settings.IGNORE_BATTERY_OPTIMIZATION_SETTINGS")

    @run_on_ui_thread
    def metti_in_secondo_piano():
        """Telefono personale: dopo la risposta l'app si toglie di mezzo."""
        try:
            PythonActivity.mActivity.moveTaskToBack(True)
        except Exception as e:
            print("Secondo piano:", e)

    def apri_impostazioni_telefono(quale="android.settings.SETTINGS"):
        try:
            Intent = autoclass("android.content.Intent")
            i = Intent(quale)
            i.addFlags(0x10000000)
            PythonActivity.mActivity.startActivity(i)
        except Exception as e:
            print("Impostazioni telefono:", e)
            if quale != "android.settings.SETTINGS":
                apri_impostazioni_telefono()

    def programma_risveglio(quando_ms):
        """Sveglia di sistema: all'ora indicata Android riaccende il telefono e apre l'app,
        anche se è in standby o bloccato."""
        try:
            from jnius import cast
            Intent = autoclass("android.content.Intent")
            PendingIntent = autoclass("android.app.PendingIntent")
            AlarmClockInfo = autoclass("android.app.AlarmManager$AlarmClockInfo")
            att = PythonActivity.mActivity
            # stesso intent che usa il componente di avvio (avvio.aar) dopo l'accensione
            intent = att.getPackageManager().getLaunchIntentForPackage(att.getPackageName())
            if intent is None:
                intent = Intent(att, att.getClass())
            intent.addFlags(0x10000000 | 0x20000000)        # NEW_TASK | SINGLE_TOP
            pi = PendingIntent.getActivity(att, 4242, intent, 0x08000000 | 0x04000000)  # UPDATE | IMMUTABLE
            am = cast("android.app.AlarmManager", att.getSystemService("alarm"))
            try:
                am.setAlarmClock(AlarmClockInfo(quando_ms, pi), pi)
            except Exception as e:
                print("setAlarmClock non riuscito, uso setExact:", e)
                am.setExactAndAllowWhileIdle(0, quando_ms, pi)   # 0 = RTC_WAKEUP
            # la ricorda per il componente che la riprogramma all'accensione del telefono
            att.getSharedPreferences("promemoria", 0).edit().putLong("prossima", quando_ms).apply()
            return True
        except Exception as e:
            print("Risveglio non programmato:", e)
            return False

    def chiedi_permessi(poi):
        """Chiede il permesso di leggere foto e audio, e SOLO DOPO apre la scelta file."""
        try:
            from android.permissions import request_permissions
            request_permissions([
                "android.permission.READ_EXTERNAL_STORAGE",
                "android.permission.READ_MEDIA_IMAGES",
                "android.permission.READ_MEDIA_AUDIO",
            ], lambda *_: Clock.schedule_once(lambda _: poi(), 0.2))
        except Exception as e:
            print("Permessi:", e)
            poi()
else:
    def imposta_schermo(sempre_acceso):
        pass

    def programma_risveglio(quando_ms):
        return False

    def apri_impostazioni_telefono(quale=""):
        avviso("Solo sul telefono", "Questo pulsante apre le impostazioni\ndi Android: sul PC non fa nulla.")

    def puo_aprirsi_da_sola():
        return True

    def batteria_senza_limiti():
        return True

    def chiedi_batteria_senza_limiti():
        avviso("Solo sul telefono", "Questo pulsante apre un'impostazione\ndi Android: sul PC non fa nulla.")

    def chiedi_apertura_da_sola():
        avviso("Solo sul telefono", "Questo pulsante apre un'impostazione\ndi Android: sul PC non fa nulla.")

    def metti_in_secondo_piano():
        print("[PC] l'app andrebbe in secondo piano")

    def chiedi_permessi(poi):
        poi()


# =====================================================
#  INVIO SMS
# =====================================================
def chiedi_permesso_sms():
    if not ANDROID:
        return True
    try:
        from android.permissions import Permission, check_permission, request_permissions
        if check_permission(Permission.SEND_SMS):
            return True
        request_permissions([Permission.SEND_SMS])
    except Exception as e:
        print("Permesso SMS:", e)
    return False


def invia_sms(numero, testo):
    """Invia un SMS. Restituisce (True, '') se partito, altrimenti (False, motivo)."""
    if not ANDROID:
        print(f"[SMS simulato a {numero}] {testo}")
        return True, "simulato sul PC"
    if not chiedi_permesso_sms():
        return False, "permesso SMS non concesso"
    try:
        SmsManager = autoclass("android.telephony.SmsManager")
        gestore = SmsManager.getDefault()
        parti = gestore.divideMessage(testo)
        if parti.size() > 1:
            gestore.sendMultipartTextMessage(numero, None, parti, None, None)
        else:
            gestore.sendTextMessage(numero, None, testo, None, None)
        return True, ""
    except Exception as e:
        return False, str(e)


def pulisci_numeri(testo):
    numeri = []
    for pezzo in testo.replace(";", ",").replace("\n", ",").split(","):
        n = "".join(c for c in pezzo if c.isdigit() or c == "+")
        if len(n) >= 6 and n not in numeri:
            numeri.append(n)
    return numeri


def _contesto_ssl():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _telegram(token, metodo, parametri=None):
    url = f"https://api.telegram.org/bot{token.strip()}/{metodo}"
    dati = urllib.parse.urlencode(parametri or {}).encode() if parametri else None
    with urllib.request.urlopen(url, data=dati, timeout=20, context=_contesto_ssl()) as r:
        return json.load(r)


def invia_telegram(token, chat_id, testo):
    """Manda un messaggio Telegram. Restituisce (True, '') oppure (False, motivo)."""
    if not token:
        return False, "manca il codice del bot"
    try:
        r = _telegram(token, "sendMessage", {"chat_id": chat_id, "text": testo})
        return (True, "") if r.get("ok") else (False, r.get("description", "errore"))
    except urllib.error.HTTPError as e:
        return False, "codice del bot sbagliato" if e.code == 401 else f"errore {e.code}"
    except Exception as e:
        return False, "niente internet" if "urlopen" in str(e) or "resolve" in str(e) else str(e)


def cerca_chat_telegram(token):
    """Chi ha scritto al bot: [{'id':..., 'nome':...}]. Restituisce (lista, errore)."""
    try:
        r = _telegram(token, "getUpdates")
    except urllib.error.HTTPError as e:
        return [], "codice del bot sbagliato" if e.code == 401 else f"errore {e.code}"
    except Exception as e:
        return [], f"niente internet ({e})"
    trovate = {}
    for u in r.get("result", []):
        msg = u.get("message") or u.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            nome = (chat.get("first_name", "") + " " + chat.get("last_name", "")).strip()
            trovate[chat["id"]] = nome or chat.get("title") or str(chat["id"])
    return [{"id": k, "nome": v} for k, v in trovate.items()], ""


AVVISI_PREDEFINITI = {"numeri": [], "presa": True, "non_confermata": True,
                      "rimandi": True, "rimandi_max": 3,
                      "sms_attivo": True, "telegram_attivo": False, "token": "", "chat": [],
                      "schermo_sempre_acceso": True, "telefono_personale": False,
                      "configurato": False}


def carica_avvisi(percorso):
    dati = dict(AVVISI_PREDEFINITI)
    if os.path.exists(percorso):
        with open(percorso, encoding="utf-8") as f:
            dati.update(json.load(f))
    return dati


# =====================================================
#  VOCE E SUONI
# =====================================================
def crea_campanello(percorso):
    """Genera un piccolo 'din-don-dan' in formato wav (nessun file esterno necessario)."""
    if os.path.exists(percorso):
        return
    freq_campioni = 22050
    dati = bytearray()
    for f in (660, 880, 1100):
        n = int(freq_campioni * 0.35)
        for i in range(n):
            smorzamento = 1 - i / n
            v = int(20000 * smorzamento * math.sin(2 * math.pi * f * i / freq_campioni))
            dati += struct.pack("<h", v)
    with wave.open(percorso, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(freq_campioni)
        w.writeframes(bytes(dati))


def crea_suoneria(percorso):
    """Suoneria di riserva (se il telefono non ne ha una): due toni alternati, 2,4 secondi
    da ripetere in continuo."""
    if os.path.exists(percorso):
        return
    fc = 22050
    dati = bytearray()
    for f, durata in ((880, 0.25), (0, 0.08), (1175, 0.25), (0, 0.08),
                      (880, 0.25), (0, 0.08), (1175, 0.25), (0, 1.16)):
        n = int(fc * durata)
        for i in range(n):
            inviluppo = min(1.0, i / 200, (n - i) / 200) if f else 0
            v = int(22000 * inviluppo * math.sin(2 * math.pi * f * i / fc)) if f else 0
            dati += struct.pack("<h", v)
    with wave.open(percorso, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fc)
        w.writeframes(bytes(dati))


class Voce:
    """Voce, messaggi registrati e suoneria. Su Android usa il canale SVEGLIA:
    si sente anche con il telefono in silenzioso, come la sveglia del telefono."""

    def __init__(self, cartella):
        self.file_campanello = os.path.join(cartella, "campanello.wav")
        crea_campanello(self.file_campanello)
        self.file_suoneria = os.path.join(cartella, "suoneria.wav")
        crea_suoneria(self.file_suoneria)
        self.mp = None          # MediaPlayer Android (voce registrata)
        self.mp_suoneria = None
        self.suono = None       # SoundLoader su PC
        self.suoneria_pc = None
        self.tts = None
        self.attr = None        # AudioAttributes "sveglia"
        self.lingua_ok = False
        self.lock_pc = threading.Lock()
        if ANDROID:
            try:
                Builder = autoclass("android.media.AudioAttributes$Builder")
                self.attr = Builder().setUsage(4).setContentType(4).build()   # USAGE_ALARM, SONIFICATION
            except Exception as e:
                print("AudioAttributes:", e)
            try:
                TextToSpeech = autoclass("android.speech.tts.TextToSpeech")
                self.tts = TextToSpeech(PythonActivity.mActivity, None)
                if self.attr:
                    self.tts.setAudioAttributes(self.attr)
            except Exception as e:
                print("TTS non disponibile:", e)

    # ---- volume SVEGLIA del telefono ----
    def alza_volume_sveglia(self):
        if not ANDROID:
            return
        try:
            from jnius import cast
            am = cast("android.media.AudioManager",
                      PythonActivity.mActivity.getSystemService("audio"))
            massimo = am.getStreamMaxVolume(4)                 # 4 = STREAM_ALARM
            minimo = max(1, int(round(massimo * VOLUME_MINIMO)))
            if am.getStreamVolume(4) < minimo:
                am.setStreamVolume(4, minimo, 0)
        except Exception as e:
            print("Volume sveglia:", e)

    # ---- suoneria ----
    def _nuovo_player(self):
        MediaPlayer = autoclass("android.media.MediaPlayer")
        mp = MediaPlayer()
        if self.attr:
            mp.setAudioAttributes(self.attr)
        return mp

    def avvia_suoneria(self, volume=VOLUME_INIZIALE):
        self.ferma_suoneria()
        if ANDROID:
            self.alza_volume_sveglia()
            mp = None
            try:
                RingtoneManager = autoclass("android.media.RingtoneManager")
                att = PythonActivity.mActivity
                uri = (RingtoneManager.getActualDefaultRingtoneUri(att, 4) or
                       RingtoneManager.getDefaultUri(4))          # 4 = TYPE_ALARM
                if uri is None:
                    raise ValueError("nessuna suoneria sveglia")
                mp = self._nuovo_player()
                mp.setDataSource(att, uri)
                mp.prepare()
            except Exception as e:
                print("Suoneria del telefono non disponibile, uso quella dell'app:", e)
                try:
                    if mp:
                        mp.release()
                    mp = self._nuovo_player()
                    mp.setDataSource(self.file_suoneria)
                    mp.prepare()
                except Exception as e2:
                    print("Suoneria:", e2)
                    return
            try:
                mp.setLooping(True)
                mp.setVolume(volume, volume)
                mp.start()
                self.mp_suoneria = mp
            except Exception as e:
                print("Suoneria:", e)
        else:
            s = SoundLoader.load(self.file_suoneria)
            if s:
                s.loop = True
                s.volume = volume
                s.play()
                self.suoneria_pc = s

    def volume_suoneria(self, volume):
        try:
            if self.mp_suoneria:
                self.mp_suoneria.setVolume(volume, volume)
            elif self.suoneria_pc:
                self.suoneria_pc.volume = volume
        except Exception:
            pass

    def pausa_suoneria(self):
        try:
            if self.mp_suoneria:
                self.mp_suoneria.pause()
            elif self.suoneria_pc:
                self.suoneria_pc.stop()
        except Exception:
            pass

    def riprendi_suoneria(self):
        try:
            if self.mp_suoneria:
                self.mp_suoneria.start()
            elif self.suoneria_pc:
                self.suoneria_pc.play()
        except Exception:
            pass

    def ferma_suoneria(self):
        if self.mp_suoneria:
            try:
                self.mp_suoneria.stop()
                self.mp_suoneria.release()
            except Exception:
                pass
            self.mp_suoneria = None
        if self.suoneria_pc:
            self.suoneria_pc.stop()
            self.suoneria_pc = None

    def sta_parlando(self):
        """True finché la voce (o il messaggio registrato) non ha finito."""
        try:
            if ANDROID:
                if self.tts and self.tts.isSpeaking():
                    return True
                return bool(self.mp and self.mp.isPlaying())
            return self.lock_pc.locked() or bool(self.suono and self.suono.state == "play")
        except Exception:
            return False

    # ---- sintesi vocale ----
    def parla(self, testo):
        if ANDROID:
            if not self.tts:
                return
            try:
                if not self.lingua_ok:
                    Locale = autoclass("java.util.Locale")
                    self.tts.setLanguage(Locale("it", "IT"))
                    self.tts.setSpeechRate(0.85)     # un po' più lenta
                    self.lingua_ok = True
                try:
                    Bundle = autoclass("android.os.Bundle")
                    parametri = Bundle()
                    parametri.putInt("streamType", 4)              # canale SVEGLIA
                    self.tts.speak(testo, 0, parametri, "promemoria")   # Android 5+
                except Exception:
                    self.tts.speak(testo, 0, None)                 # telefoni più vecchi
            except Exception as e:
                print("Errore voce:", e)
        else:
            threading.Thread(target=self._parla_pc, args=(testo,), daemon=True).start()

    def _parla_pc(self, testo):
        with self.lock_pc:
            try:
                import pyttsx3
                motore = pyttsx3.init()
                motore.say(testo)
                motore.runAndWait()
            except Exception:
                print("VOCE:", testo)

    def zitto(self):
        if ANDROID and self.tts:
            try:
                self.tts.stop()
            except Exception:
                pass

    # ---- file audio ----
    def suona(self, percorso):
        self.ferma_audio()
        try:
            if ANDROID:
                self.mp = self._nuovo_player()
                self.mp.setDataSource(percorso)
                self.mp.prepare()
                self.mp.start()
            else:
                self.suono = SoundLoader.load(percorso)
                if self.suono:
                    self.suono.play()
        except Exception as e:
            print("Errore audio:", e)

    def ferma_audio(self):
        if self.mp:
            try:
                self.mp.release()
            except Exception:
                pass
            self.mp = None
        if self.suono:
            self.suono.stop()
            self.suono = None

    def campanello(self):
        self.suona(self.file_campanello)


# =====================================================
#  DATI
# =====================================================
def persone_esempio():
    """Al primo avvio c'è una sola persona, da rinominare nella configurazione guidata."""
    return [
        {"id": "p1", "nome": "Utente", "colore": "azzurro"},
    ]


def carica_persone(percorso):
    if not os.path.exists(percorso):
        persone = persone_esempio()
        salva_medicine(percorso, persone)
        return persone
    with open(percorso, encoding="utf-8") as f:
        return json.load(f)


ESEMPI = {("Pastiglia della pressione", "1 compressa con un bicchiere d'acqua"),
          ("Vitamina D", "10 gocce")}


def e_esempio(med):
    return bool(med.get("esempio")) or (med.get("nome"), med.get("dose")) in ESEMPI


def colori_di(persona):
    """(forte, chiaro, sfondo sveglia) del colore scelto per la persona."""
    return PALETTE.get((persona or {}).get("colore"), PALETTE["verde"])


def medicine_esempio():
    """Al primo avvio nessuna medicina: si aggiungono alla fine della configurazione guidata."""
    return []


def carica_medicine(percorso):
    if not os.path.exists(percorso):
        meds = medicine_esempio()
        salva_medicine(percorso, meds)
        return meds
    with open(percorso, encoding="utf-8") as f:
        return json.load(f)


def salva_medicine(percorso, meds):
    tmp = percorso + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meds, f, ensure_ascii=False, indent=2)
    os.replace(tmp, percorso)


INTESTAZIONE_REG = ["data", "ora_prevista", "ora_risposta", "medicina", "dose", "esito", "id",
                    "persona"]


def aggiorna_registro_vecchio(percorso):
    """Il registro delle versioni precedenti non aveva la colonna 'persona': la aggiunge."""
    if not os.path.exists(percorso):
        return
    with open(percorso, encoding="utf-8") as f:
        righe = list(csv.reader(f, delimiter=";"))
    if not righe or "persona" in righe[0]:
        return
    with open(percorso, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(INTESTAZIONE_REG)
        for r in righe[1:]:
            w.writerow((r + [""] * 8)[:8])


def registra(percorso, med, orario, esito, persona=""):
    nuovo = not os.path.exists(percorso)
    ora = dt.datetime.now()
    with open(percorso, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        if nuovo:
            w.writerow(INTESTAZIONE_REG)
        w.writerow([ora.date().isoformat(), orario, ora.strftime("%H:%M"),
                    med["nome"], med["dose"], esito, med["id"], persona])


def leggi_registro(percorso):
    if not os.path.exists(percorso):
        return []
    with open(percorso, encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


def stato_di_oggi(percorso):
    stato = {}
    oggi = dt.date.today().isoformat()
    for r in leggi_registro(percorso):
        if r.get("data") == oggi and r.get("esito") in ("presa", "non confermata"):
            stato[(r["id"], r["ora_prevista"], oggi)] = r["esito"]
    return stato


def orario_valido(s):
    try:
        dt.datetime.strptime(s.strip(), "%H:%M")
        return True
    except ValueError:
        return False


def normalizza_orari(testo):
    parti = [p.strip().replace(".", ":") for p in testo.replace(";", ",").split(",") if p.strip()]
    if not parti or not all(orario_valido(p) for p in parti):
        return None
    return sorted({dt.datetime.strptime(p, "%H:%M").strftime("%H:%M") for p in parti})


# =====================================================
#  PICCOLI AIUTI GRAFICI
# =====================================================
def sfondo(widget, colore):
    with widget.canvas.before:
        c = Color(*hexc(colore))
        r = Rectangle(pos=widget.pos, size=widget.size)
    widget.bind(pos=lambda *_: setattr(r, "pos", widget.pos),
                size=lambda *_: setattr(r, "size", widget.size))
    return c


def etichetta(testo, size=20, bold=False, colore=TESTO, altezza=None, halign="center"):
    l = Label(text=testo, font_size=sp(size), bold=bold, color=hexc(colore),
              halign=halign, valign="middle", size_hint_y=None)
    if altezza is None:
        l.bind(width=lambda i, w: setattr(i, "text_size", (w, None)),
               texture_size=lambda i, t: setattr(i, "height", t[1] + dp(8)))
    else:
        l.height = altezza
        l.bind(size=lambda i, s: setattr(i, "text_size", s))
    return l


def bottone(testo, colore, size=22, altezza=None, azione=None):
    b = Button(text=testo, font_size=sp(size), bold=True, background_normal="",
               background_color=hexc(colore), color=(1, 1, 1, 1), halign="center",
               size_hint_y=None, height=altezza or dp(70))
    b.bind(size=lambda i, s: setattr(i, "text_size", (s[0] - dp(12), None)))
    if azione:
        b.bind(on_release=lambda *_: azione())
    return b


def avviso(titolo, testo):
    box = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(10))
    box.add_widget(etichetta(testo, 18, colore="#FFFFFF"))
    p = Popup(title=titolo, content=box, size_hint=(0.9, None), height=dp(260))
    box.add_widget(bottone("OK", BLU, azione=p.dismiss))
    p.open()


def cartelle_rapide():
    """Pulsanti per saltare subito alle cartelle dove di solito stanno foto e registrazioni."""
    if ANDROID:
        r = "/storage/emulated/0"
        candidati = [
            ("Fotocamera", [r + "/DCIM/Camera", r + "/DCIM"]),
            ("Download", [r + "/Download"]),
            ("Immagini", [r + "/Pictures"]),
            ("Registrazioni", [r + "/Recordings", r + "/Music", r + "/Voice Recorder"]),
            ("Tutto", [r]),
        ]
    else:
        h = os.path.expanduser("~")
        candidati = [
            ("Immagini", [os.path.join(h, "OneDrive", "Immagini"), os.path.join(h, "Pictures")]),
            ("Download", [os.path.join(h, "Downloads")]),
            ("Desktop", [os.path.join(h, "OneDrive", "Desktop"), os.path.join(h, "Desktop")]),
            ("Documenti", [os.path.join(h, "OneDrive", "Documenti"), os.path.join(h, "Documents")]),
        ]
    risultato = []
    for nome, percorsi in candidati:
        for p in percorsi:
            if os.path.isdir(p):
                risultato.append((nome, p))
                break
    return risultato


def scegli_file(titolo, estensioni, callback):
    chiedi_permessi(lambda: _apri_scelta_file(titolo, estensioni, callback))


def _apri_scelta_file(titolo, estensioni, callback):
    rapide = cartelle_rapide()
    inizio = rapide[0][1] if rapide else ("/storage/emulated/0" if ANDROID else os.path.expanduser("~"))
    if not os.path.isdir(inizio):
        inizio = "/"
    fc = FileChooserListView(
        path=inizio,
        filters=[lambda cartella, nome: nome.lower().endswith(estensioni)])
    box = BoxLayout(orientation="vertical", spacing=dp(6))

    # pulsanti cartelle rapide
    riga_cartelle = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(4))
    for nome, percorso in rapide:
        riga_cartelle.add_widget(bottone(nome, BLU, 14, dp(52),
                                         lambda p=percorso: setattr(fc, "path", p)))
    box.add_widget(riga_cartelle)

    # cartella attuale, così si capisce dove si è
    lbl_cartella = etichetta(inizio, 13, colore="#FFFFFF", halign="left")
    fc.bind(path=lambda _, p: setattr(lbl_cartella, "text", p))
    box.add_widget(lbl_cartella)
    box.add_widget(fc)
    riga = BoxLayout(size_hint_y=None, height=dp(64), spacing=dp(8))
    p = Popup(title=titolo, content=box, size_hint=(0.98, 0.95))

    def conferma():
        if fc.selection:
            p.dismiss()
            callback(fc.selection[0])

    riga.add_widget(bottone("Annulla", GRIGIO, azione=p.dismiss))
    riga.add_widget(bottone("Scegli", VERDE, azione=conferma))
    box.add_widget(riga)
    p.open()


class TastoLungo(Button):
    """Pulsante che agisce solo se tenuto premuto 2 secondi (protegge le impostazioni)."""

    def __init__(self, azione, **kw):
        super().__init__(**kw)
        self.azione = azione
        self._ev = None

    def on_touch_down(self, tocco):
        if self.collide_point(*tocco.pos):
            self._ev = Clock.schedule_once(lambda _: self.azione(), 2)
        return super().on_touch_down(tocco)

    def on_touch_up(self, tocco):
        if self._ev:
            self._ev.cancel()
            self._ev = None
        return super().on_touch_up(tocco)


# =====================================================
#  SCHERMATA DELLA SVEGLIA
# =====================================================
class Allarme(ModalView):
    def __init__(self, app, med, orario, chiave, **kw):
        self.persona = app.persona_di(med)
        self.forte, _, self.fondo = colori_di(self.persona)
        super().__init__(size_hint=(1, 1), auto_dismiss=False,
                         background="", background_color=hexc(self.fondo), **kw)
        self.app, self.med, self.orario, self.chiave = app, med, orario, chiave
        self.inizio = dt.datetime.now()
        self.acceso = True

        box = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(10))
        sfondo(box, self.fondo)

        if self.persona:
            fascia = Label(text=self.persona["nome"].upper(), font_size=sp(40), bold=True,
                           color=(1, 1, 1, 1), size_hint_y=None, height=dp(70))
            sfondo(fascia, self.forte)
            box.add_widget(fascia)
        self.titolo = etichetta("È L'ORA DELLA MEDICINA", 28, True, ROSSO)
        box.add_widget(self.titolo)
        if orario != "PROVA":
            box.add_widget(etichetta(f"ore {orario}", 24))

        foto = med.get("foto")
        if foto and os.path.exists(foto):
            box.add_widget(Image(source=foto, fit_mode="contain"))
        else:
            box.add_widget(BoxLayout())   # spazio vuoto elastico

        box.add_widget(etichetta(med["nome"], 34, True))
        box.add_widget(etichetta(med["dose"], 24))

        box.add_widget(bottone("L'HO PRESA", VERDE, 34, dp(120),
                               lambda: self.chiudi("presa")))
        riga = BoxLayout(size_hint_y=None, height=dp(90), spacing=dp(10))
        riga.add_widget(bottone(f"RICORDAMELO\nTRA {RIMANDA_MIN} MINUTI", ARANCIO, 20,
                                dp(90), self.rimanda))
        riga.add_widget(bottone("RIPETI", BLU, 22, dp(90), self._passa_a_voce))
        box.add_widget(riga)
        self.add_widget(box)

        # suoneria (volume crescente) e voce si alternano finché non si risponde
        self.fase = "squillo"
        self.durata_squillo = PRIMO_SQUILLO_SEC
        self.inizio_fase = dt.datetime.now()
        app.voce.avvia_suoneria(VOLUME_INIZIALE)
        self.ev_tick = Clock.schedule_interval(lambda _: self._tick(), 0.5)
        self.ev_lampeggio = Clock.schedule_interval(lambda _: self._lampeggia(), 0.7)

    def messaggio(self):
        if self.med.get("messaggio"):
            return self.med["messaggio"]
        nome = f"{self.persona['nome']}, " if self.persona else ""
        return (f"{nome}è ora di prendere {self.med['nome']}. {self.med['dose']}. "
                f"Quando l'hai presa, premi il pulsante verde.")

    def annuncia_una_volta(self):
        audio = self.med.get("audio")
        if audio and os.path.exists(audio):
            self.app.voce.suona(audio)
        else:
            self.app.voce.parla(self.messaggio())

    def _tick(self):
        adesso = dt.datetime.now()
        if adesso - self.inizio > dt.timedelta(minutes=TIMEOUT_MIN):
            self.chiudi("non confermata")
            return
        trascorso = (adesso - self.inizio_fase).total_seconds()
        if self.fase == "squillo":
            totale = (adesso - self.inizio).total_seconds()
            volume = min(1.0, VOLUME_INIZIALE + (1 - VOLUME_INIZIALE) * totale / CRESCENDO_SEC)
            self.app.voce.volume_suoneria(volume)
            if trascorso >= self.durata_squillo:
                self._passa_a_voce()
        elif (trascorso > 2.5 and not self.app.voce.sta_parlando()) or trascorso > 30:
            self.fase = "squillo"
            self.durata_squillo = SQUILLO_SEC
            self.inizio_fase = adesso
            self.app.voce.riprendi_suoneria()

    def _passa_a_voce(self):
        self.app.voce.pausa_suoneria()
        self.app.voce.zitto()
        self.annuncia_una_volta()
        self.fase = "voce"
        self.inizio_fase = dt.datetime.now()

    def _lampeggia(self):
        self.acceso = not self.acceso
        self.titolo.color = hexc(ROSSO) if self.acceso else hexc(self.fondo)

    def _ferma(self):
        self.ev_tick.cancel()
        self.ev_lampeggio.cancel()
        self.app.voce.ferma_suoneria()
        self.app.voce.zitto()
        self.app.voce.ferma_audio()

    def chiudi(self, esito):
        self._ferma()
        self.dismiss()
        self.app.esito(self.chiave, self.med, self.orario, esito)

    def rimanda(self):
        self._ferma()
        self.dismiss()
        self.app.rimanda(self.chiave, self.med, self.orario)


# =====================================================
#  SCHERMATA PRINCIPALE
# =====================================================
class SchermataPrincipale(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(6))
        sfondo(box, BG)
        self.lbl_ora = etichetta("", 90, True, altezza=dp(130))
        self.lbl_data = etichetta("", 24)
        box.add_widget(self.lbl_ora)
        box.add_widget(self.lbl_data)
        box.add_widget(etichetta("Medicine di oggi", 24, True))

        scroll = ScrollView()
        self.lista = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        self.lista.bind(minimum_height=self.lista.setter("height"))
        scroll.add_widget(self.lista)
        box.add_widget(scroll)

        box.add_widget(TastoLungo(
            text="Impostazioni (tieni premuto)", font_size=sp(13),
            size_hint_y=None, height=dp(40), background_normal="",
            background_color=hexc("#E6DCC6"), color=hexc(GRIGIO),
            azione=lambda: App.get_running_app().vai("gestione")))
        self.add_widget(box)
        self.aggiorna_ora()
        Clock.schedule_interval(lambda _: self.aggiorna_ora(), 1)

    def aggiorna_ora(self):
        ora = dt.datetime.now()
        self.lbl_ora.text = ora.strftime("%H:%M")
        self.lbl_data.text = (f"{GIORNI[ora.weekday()].capitalize()} {ora.day} "
                              f"{MESI[ora.month - 1]} {ora.year}")

    def aggiorna_lista(self):
        app = App.get_running_app()
        self.lista.clear_widgets()
        oggi = dt.date.today().isoformat()
        adesso = dt.datetime.now().strftime("%H:%M")
        if not app.medicine:
            self.lista.add_widget(etichetta("Nessuna medicina impostata", 20))
            return
        # un gruppo per ogni persona, nell'ordine dell'elenco persone; poi le medicine senza persona
        gruppi = [(p, [m for m in app.medicine if m.get("persona") == p["id"]])
                  for p in app.persone]
        ids = {p["id"] for p in app.persone}
        gruppi.append((None, [m for m in app.medicine if m.get("persona") not in ids]))
        for persona, meds in gruppi:
            if not meds:
                continue
            forte, chiaro, _ = colori_di(persona)
            if persona:
                fascia = Label(text=persona["nome"].upper(), font_size=sp(22), bold=True,
                               color=(1, 1, 1, 1), size_hint_y=None, height=dp(40))
                sfondo(fascia, forte)
                self.lista.add_widget(fascia)
            righe = sorted(((o, m) for m in meds for o in m["orari"]), key=lambda x: x[0])
            for orario, m in righe:
                self.lista.add_widget(self._riga(app, orario, m, oggi, adesso, forte, chiaro))
            self.lista.add_widget(Label(size_hint_y=None, height=dp(6)))

    def _riga(self, app, orario, m, oggi, adesso, forte, chiaro):
        k = (m["id"], orario, oggi)
        esito = app.stato.get(k)
        if esito == "presa":
            stato, colore = "PRESA", VERDE
        elif esito == "non confermata":
            stato, colore = "NON\nCONFERMATA", ROSSO
        elif k in app.rimandi:
            stato, colore = "TRA POCO", ARANCIO
        elif orario > adesso:
            stato, colore = "da prendere", GRIGIO
        else:
            stato, colore = "", GRIGIO
        riga = BoxLayout(size_hint_y=None, height=dp(72), spacing=dp(6), padding=(0, dp(4)))
        sfondo(riga, chiaro)
        striscia = Label(size_hint=(None, 1), width=dp(8))
        sfondo(striscia, forte)
        o = etichetta(orario, 24, True, forte)
        o.size_hint_y = 1
        o.size_hint_x = 0.25
        n = etichetta(f"{m['nome']}\n[size={int(sp(14))}]{m['dose']}[/size]", 18, halign="left")
        n.markup = True
        n.size_hint_x = 0.48
        n.bind(height=lambda i, h, riga=riga: setattr(riga, "height", max(dp(72), h + dp(8))))
        st = etichetta(stato, 14, True, colore)
        st.size_hint_y = 1
        st.size_hint_x = 0.25
        for w in (striscia, o, n, st):
            riga.add_widget(w)
        return riga


# =====================================================
#  IMPOSTAZIONI: ELENCO MEDICINE
# =====================================================
class SchermataGestione(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        sfondo(box, BG)
        box.add_widget(etichetta("Impostazioni", 28, True))
        self.lbl_sotto = etichetta("Tocca una medicina per modificarla", 15, colore=GRIGIO)
        box.add_widget(self.lbl_sotto)
        scroll = ScrollView()
        self.lista = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        self.lista.bind(minimum_height=self.lista.setter("height"))
        scroll.add_widget(self.lista)
        box.add_widget(scroll)
        app = App.get_running_app()
        box.add_widget(bottone("+ Aggiungi medicina", VERDE, 20, dp(60),
                               lambda: app.modifica(None)))
        r = BoxLayout(size_hint_y=None, height=dp(60), spacing=dp(8))
        r.add_widget(bottone("Persone", "#7B4FB8", 20, dp(60), lambda: app.vai("persone")))
        r.add_widget(bottone("Storico", BLU, 20, dp(60), lambda: app.vai("storico")))
        box.add_widget(r)
        box.add_widget(bottone("Avvisi ai familiari (SMS / Telegram)", "#00897B", 18, dp(60),
                               lambda: app.vai("avvisi")))
        # compare solo se c'è qualcosa da sistemare
        self.btn_apertura = bottone("", ROSSO, 15, dp(44), self._apertura)
        box.add_widget(self.btn_apertura)
        r = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(8))
        r.add_widget(bottone("Configurazione guidata", "#7B4FB8", 15, dp(50),
                             lambda: app.avvia_guida()))
        r.add_widget(bottone("Impostazioni del telefono", "#5A5A5A", 14, dp(50),
                             apri_impostazioni_telefono))
        box.add_widget(r)
        box.add_widget(bottone("Torna all'orologio", GRIGIO, 20, dp(60),
                               lambda: app.vai("principale")))
        self.add_widget(box)

    def _testo_schermo(self):
        app = App.get_running_app()
        tipo = ("Telefono personale" if app.avvisi.get("telefono_personale")
                else "Telefono dedicato")
        self.lbl_sotto.text = f"{tipo} · tocca una medicina per modificarla"
        problemi = []
        if not puo_aprirsi_da_sola():
            problemi.append("apertura automatica")
        if not batteria_senza_limiti():
            problemi.append("batteria")
        if problemi:
            self.btn_apertura.text = "Da sistemare: " + " e ".join(problemi) + " (tocca qui)"
            self.btn_apertura.height = dp(44)
            self.btn_apertura.opacity = 1
            self.btn_apertura.disabled = False
        else:
            self.btn_apertura.height = 0
            self.btn_apertura.opacity = 0
            self.btn_apertura.disabled = True

    def _apertura(self):
        if not puo_aprirsi_da_sola():
            avviso("Apertura automatica", "Si apre un'impostazione di Android:\n"
                                          "attiva «Promemoria Medicine»\npoi torna indietro.")
            Clock.schedule_once(lambda _: chiedi_apertura_da_sola(), 2.5)
        elif not batteria_senza_limiti():
            chiedi_batteria_senza_limiti()

    def on_enter(self, *_):
        self._testo_schermo()      # aggiorna lo stato dopo il ritorno dalle impostazioni

    def on_pre_enter(self, *_):
        app = App.get_running_app()
        self._testo_schermo()
        self.lista.clear_widgets()
        for i, m in enumerate(app.medicine):
            persona = app.persona_di(m)
            chi = persona["nome"] if persona else "nessuna persona"
            forte = colori_di(persona)[0] if persona else "#5A5A5A"
            self.lista.add_widget(bottone(f"{m['nome']}  ({chi})\n{', '.join(m['orari'])}",
                                          forte, 18, dp(72), lambda i=i: app.modifica(i)))


# =====================================================
#  IMPOSTAZIONI: MODIFICA DI UNA MEDICINA
# =====================================================
class SchermataModifica(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.indice = None
        self.foto = ""
        self.audio = ""

        esterno = BoxLayout()
        sfondo(esterno, BG)
        scroll = ScrollView()   # resta solo per i telefoni con schermo molto piccolo
        box = BoxLayout(orientation="vertical", size_hint_y=None, padding=dp(10), spacing=dp(5))
        box.bind(minimum_height=box.setter("height"))

        self.titolo = etichetta("Medicina", 22, True)
        box.add_widget(self.titolo)

        # --- campi di testo, compatti ---
        self.campi = {}
        for chiave, testo, alto in [
            ("nome", "Nome della medicina", False),
            ("dose", "Dose / istruzioni", False),
            ("orari", "Orari (es. 08:00, 20:00)", False),
            ("messaggio", "Frase da dire (facoltativa)", True),
        ]:
            box.add_widget(etichetta(testo, 13, halign="left", altezza=dp(18)))
            ti = TextInput(multiline=alto, font_size=sp(17), size_hint_y=None,
                           height=dp(62) if alto else dp(40), padding=(dp(8), dp(8)))
            box.add_widget(ti)
            self.campi[chiave] = ti

        # --- persona: un pulsante per ogni persona ---
        box.add_widget(etichetta("Per chi è", 13, True, halign="left", altezza=dp(20)))
        self.riga_persone = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        box.add_widget(self.riga_persone)
        self.persona_id = None

        # --- foto: anteprima a sinistra, pulsanti a destra ---
        box.add_widget(etichetta("Foto della medicina", 13, True, halign="left", altezza=dp(20)))
        r = BoxLayout(size_hint_y=None, height=dp(96), spacing=dp(6))
        cornice = BoxLayout(size_hint_x=0.42)
        sfondo(cornice, "#FFFFFF")
        self.anteprima = Image(fit_mode="contain")
        self.lbl_nofoto = etichetta("nessuna\nfoto", 13, colore=GRIGIO)
        self.lbl_nofoto.size_hint_y = 1
        cornice.add_widget(self.anteprima)
        self.cornice = cornice
        r.add_widget(cornice)
        col = BoxLayout(orientation="vertical", spacing=dp(6))
        col.add_widget(bottone("Scegli foto", BLU, 15, dp(45), self.scegli_foto))
        col.add_widget(bottone("Togli foto", GRIGIO, 15, dp(45), self.togli_foto))
        r.add_widget(col)
        box.add_widget(r)

        # --- messaggio vocale ---
        box.add_widget(etichetta("Messaggio vocale registrato", 13, True, halign="left",
                                 altezza=dp(20)))
        self.lbl_audio = etichetta("", 12, colore=GRIGIO, altezza=dp(18))
        box.add_widget(self.lbl_audio)
        r = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        r.add_widget(bottone("Scegli", BLU, 15, dp(44), self.scegli_audio))
        r.add_widget(bottone("Ascolta", BLU, 15, dp(44), self.ascolta))
        r.add_widget(bottone("Togli", GRIGIO, 15, dp(44), self.togli_audio))
        box.add_widget(r)

        # --- azioni: due per riga ---
        box.add_widget(etichetta("", 4, altezza=dp(4)))
        r = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(6))
        r.add_widget(bottone("SALVA", VERDE, 18, dp(52), self.salva))
        r.add_widget(bottone("Prova la sveglia", ARANCIO, 15, dp(52), self.prova))
        box.add_widget(r)
        r = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
        r.add_widget(bottone("Elimina", ROSSO, 15, dp(46), self.elimina))
        r.add_widget(bottone("Indietro", GRIGIO, 15, dp(46),
                             lambda: App.get_running_app().vai("gestione")))
        box.add_widget(r)

        scroll.add_widget(box)
        esterno.add_widget(scroll)
        self.add_widget(esterno)

    # ---- caricamento ----
    def carica(self, indice):
        app = App.get_running_app()
        self.indice = indice
        m = app.medicine[indice] if indice is not None else {}
        self.titolo.text = "Modifica medicina" if indice is not None else "Nuova medicina"
        self.campi["nome"].text = m.get("nome", "")
        self.campi["dose"].text = m.get("dose", "")
        self.campi["orari"].text = ", ".join(m.get("orari", []))
        self.campi["messaggio"].text = m.get("messaggio", "")
        self.foto = m.get("foto", "")
        self.audio = m.get("audio", "")
        ids = [p["id"] for p in app.persone]
        self.persona_id = m.get("persona") if m.get("persona") in ids else (ids[0] if ids else None)
        self._mostra()

    def _disegna_persone(self):
        app = App.get_running_app()
        self.riga_persone.clear_widgets()
        if not app.persone:
            self.riga_persone.add_widget(etichetta("Aggiungi le persone dalle Impostazioni", 13,
                                                   colore=GRIGIO, altezza=dp(44)))
        for p in app.persone:
            forte, chiaro, _ = colori_di(p)
            scelta = p["id"] == self.persona_id
            b = bottone(("• " + p["nome"] + " •") if scelta else p["nome"], forte if scelta else chiaro,
                        15, dp(44), lambda pid=p["id"]: self._scegli_persona(pid))
            if not scelta:
                b.color = hexc(forte)
            self.riga_persone.add_widget(b)

    def _scegli_persona(self, pid):
        self.persona_id = pid
        self._disegna_persone()

    def _mostra(self):
        self._disegna_persone()
        ce_foto = bool(self.foto and os.path.exists(self.foto))
        self.cornice.clear_widgets()
        if ce_foto:
            self.anteprima.source = self.foto
            self.cornice.add_widget(self.anteprima)
        else:
            self.anteprima.source = ""
            self.cornice.add_widget(self.lbl_nofoto)
        self.lbl_audio.text = ("Messaggio vocale impostato" if self.audio
                               else "Nessuno: userà la voce del telefono")

    # ---- foto e audio ----
    def scegli_foto(self):
        scegli_file("Scegli la foto", EST_FOTO, self._foto_scelta)

    def _foto_scelta(self, percorso):
        self.foto = App.get_running_app().copia_in_app(percorso)
        self._mostra()

    def togli_foto(self):
        self.foto = ""
        self._mostra()

    def scegli_audio(self):
        scegli_file("Scegli il messaggio vocale", EST_AUDIO, self._audio_scelto)

    def _audio_scelto(self, percorso):
        self.audio = App.get_running_app().copia_in_app(percorso)
        self._mostra()

    def togli_audio(self):
        self.audio = ""
        self._mostra()

    def ascolta(self):
        app = App.get_running_app()
        if self.audio and os.path.exists(self.audio):
            app.voce.suona(self.audio)
        else:
            nome = self.campi["nome"].text.strip() or "la medicina"
            app.voce.parla(self.campi["messaggio"].text.strip() or f"È ora di prendere {nome}.")

    # ---- salvataggio ----
    def _dati(self):
        nome = self.campi["nome"].text.strip()
        if not nome:
            avviso("Attenzione", "Scrivi il nome della medicina.")
            return None
        orari = normalizza_orari(self.campi["orari"].text)
        if not orari:
            avviso("Attenzione", "Orari non validi.\nScrivi per esempio: 08:00, 20:30")
            return None
        return {"nome": nome, "dose": self.campi["dose"].text.strip(), "orari": orari,
                "messaggio": self.campi["messaggio"].text.strip(),
                "foto": self.foto, "audio": self.audio, "persona": self.persona_id}

    def salva(self):
        dati = self._dati()
        if not dati:
            return
        app = App.get_running_app()
        if self.indice is None:
            dati["id"] = str(uuid.uuid4())
            app.medicine.append(dati)
            self.indice = len(app.medicine) - 1
        else:
            app.medicine[self.indice].update(dati)
        app.salva()
        avviso("Salvato", f"«{dati['nome']}» è stata salvata.")

    def prova(self):
        dati = self._dati()
        if not dati:
            return
        dati["id"] = "prova"
        app = App.get_running_app()
        app.apri_allarme(dati, "PROVA", ("prova", "PROVA", "prova"))

    def elimina(self):
        if self.indice is None:
            App.get_running_app().vai("gestione")
            return
        app = App.get_running_app()
        nome = app.medicine[self.indice]["nome"]
        box = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(10))
        box.add_widget(etichetta(f"Eliminare «{nome}»?", 20, colore="#FFFFFF"))
        p = Popup(title="Elimina", content=box, size_hint=(0.9, None), height=dp(250))

        def si():
            p.dismiss()
            del app.medicine[self.indice]
            app.salva()
            app.vai("gestione")

        riga = BoxLayout(size_hint_y=None, height=dp(64), spacing=dp(8))
        riga.add_widget(bottone("No", GRIGIO, azione=p.dismiss))
        riga.add_widget(bottone("Sì, elimina", ROSSO, azione=si))
        box.add_widget(riga)
        p.open()


# =====================================================
#  IMPOSTAZIONI: PERSONE
# =====================================================
class SchermataPersone(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        sfondo(box, BG)
        box.add_widget(etichetta("Persone", 28, True))
        box.add_widget(etichetta("Tocca una persona per cambiarle nome o colore", 15,
                                 colore=GRIGIO))
        scroll = ScrollView()
        self.lista = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        self.lista.bind(minimum_height=self.lista.setter("height"))
        scroll.add_widget(self.lista)
        box.add_widget(scroll)
        app = App.get_running_app()
        box.add_widget(bottone("+ Aggiungi persona", VERDE, 20, dp(60),
                               lambda: app.modifica_persona(None)))
        box.add_widget(bottone("Indietro", GRIGIO, 20, dp(60), lambda: app.vai("gestione")))
        self.add_widget(box)

    def on_pre_enter(self, *_):
        app = App.get_running_app()
        self.lista.clear_widgets()
        for i, p in enumerate(app.persone):
            n = sum(1 for m in app.medicine if m.get("persona") == p["id"])
            self.lista.add_widget(bottone(f"{p['nome']}\n{n} medicin{'a' if n == 1 else 'e'}",
                                          colori_di(p)[0], 20, dp(72),
                                          lambda i=i: app.modifica_persona(i)))


class SchermataPersona(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.indice = None
        self.colore = "rosa"
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        self.fondo = sfondo(box, BG)
        self.titolo = etichetta("Persona", 26, True)
        box.add_widget(self.titolo)
        box.add_widget(etichetta("Nome", 15, halign="left", altezza=dp(22)))
        self.nome = TextInput(multiline=False, font_size=sp(22), size_hint_y=None, height=dp(52))
        box.add_widget(self.nome)
        box.add_widget(etichetta("Colore", 15, halign="left", altezza=dp(22)))
        self.griglia = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(126),
                                 spacing=dp(6))
        box.add_widget(self.griglia)
        box.add_widget(etichetta("Anteprima", 15, halign="left", altezza=dp(22)))
        self.anteprima = Label(font_size=sp(22), bold=True, color=(1, 1, 1, 1),
                               size_hint_y=None, height=dp(44))
        self.sfondo_anteprima = sfondo(self.anteprima, "#888888")
        box.add_widget(self.anteprima)
        self.riga_anteprima = Label(text="08:00   Medicina di esempio", font_size=sp(17),
                                    size_hint_y=None, height=dp(50))
        self.sfondo_riga = sfondo(self.riga_anteprima, "#FFFFFF")
        box.add_widget(self.riga_anteprima)
        box.add_widget(BoxLayout())
        app = App.get_running_app()
        box.add_widget(bottone("SALVA", VERDE, 20, dp(58), self.salva))
        r = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(8))
        r.add_widget(bottone("Elimina", ROSSO, 17, dp(52), self.elimina))
        r.add_widget(bottone("Indietro", GRIGIO, 17, dp(52), lambda: app.vai("persone")))
        box.add_widget(r)
        self.nome.bind(text=lambda *_: self._aggiorna())
        self.add_widget(box)

    def carica(self, indice):
        app = App.get_running_app()
        self.indice = indice
        if indice is None:
            usati = {p.get("colore") for p in app.persone}
            liberi = [c for c in PALETTE if c not in usati]
            self.colore = liberi[0] if liberi else "verde"
            self.nome.text = ""
            self.titolo.text = "Nuova persona"
        else:
            p = app.persone[indice]
            self.colore = p.get("colore", "verde")
            self.nome.text = p["nome"]
            self.titolo.text = "Modifica persona"
        self._aggiorna()

    def _aggiorna(self):
        self.griglia.clear_widgets()
        nomi = list(PALETTE)
        for riga in (nomi[:3], nomi[3:]):
            r = BoxLayout(spacing=dp(6))
            for c in riga:
                forte = PALETTE[c][0]
                r.add_widget(bottone(("• " + c + " •") if c == self.colore else c, forte, 17, dp(60),
                                     lambda c=c: self._scegli(c)))
            self.griglia.add_widget(r)
        forte, chiaro, _ = PALETTE[self.colore]
        self.anteprima.text = (self.nome.text.strip() or "Nome").upper()
        self.sfondo_anteprima.rgba = hexc(forte)
        self.sfondo_riga.rgba = hexc(chiaro)
        self.riga_anteprima.color = hexc(forte)

    def _scegli(self, c):
        self.colore = c
        self._aggiorna()

    def salva(self):
        nome = self.nome.text.strip()
        if not nome:
            avviso("Attenzione", "Scrivi il nome della persona.")
            return
        app = App.get_running_app()
        if self.indice is None:
            app.persone.append({"id": uuid.uuid4().hex[:8], "nome": nome, "colore": self.colore})
            self.indice = len(app.persone) - 1
        else:
            app.persone[self.indice].update({"nome": nome, "colore": self.colore})
        app.salva_persone()
        avviso("Salvato", f"«{nome}» è stata salvata.")

    def elimina(self):
        app = App.get_running_app()
        if self.indice is None:
            app.vai("persone")
            return
        p = app.persone[self.indice]
        n = sum(1 for m in app.medicine if m.get("persona") == p["id"])
        if n:
            avviso("Non si può eliminare",
                   f"{p['nome']} ha ancora {n} medicin{'a' if n == 1 else 'e'}.\n"
                   f"Prima eliminale o assegnale a un'altra persona.")
            return
        del app.persone[self.indice]
        app.salva_persone()
        app.vai("persone")


# =====================================================
#  IMPOSTAZIONI: AVVISI AI FAMILIARI (SMS e TELEGRAM)
# =====================================================
class SchermataAvvisi(Screen):
    VOCI = [
        ("presa", "Quando una dose viene presa"),
        ("non_confermata", f"Quando una dose NON è confermata\n(nessuna risposta in {TIMEOUT_MIN} minuti)"),
        ("rimandi", "Quando una dose viene rimandata\npiù volte di seguito"),
    ]

    def _interruttore(self, box, chiave, testo, alto=dp(54)):
        r = BoxLayout(size_hint_y=None, height=alto, spacing=dp(8))
        l = etichetta(testo, 15, halign="left")
        l.size_hint_y = 1
        r.add_widget(l)
        b = bottone("", GRIGIO, 17, alto, lambda k=chiave: self._cambia(k))
        b.size_hint_x = None
        b.width = dp(90)
        r.add_widget(b)
        self.interruttori[chiave] = b
        box.add_widget(r)

    def _titolo(self, box, testo):
        t = Label(text=testo, font_size=sp(17), bold=True, color=(1, 1, 1, 1),
                  size_hint_y=None, height=dp(34))
        sfondo(t, "#00897B")
        box.add_widget(t)

    def __init__(self, **kw):
        super().__init__(**kw)
        esterno = BoxLayout()
        sfondo(esterno, BG)
        scroll = ScrollView()
        box = BoxLayout(orientation="vertical", size_hint_y=None, padding=dp(12), spacing=dp(6))
        box.bind(minimum_height=box.setter("height"))
        self.interruttori = {}

        box.add_widget(etichetta("Avvisi ai familiari", 24, True))

        # --- quando ---
        self._titolo(box, "QUANDO AVVISARE")
        for chiave, testo in self.VOCI:
            self._interruttore(box, chiave, testo)
        r = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(8))
        l = etichetta("Rinvii prima dell'avviso", 15, halign="left")
        l.size_hint_y = 1
        r.add_widget(l)
        r.add_widget(bottone("-", GRIGIO, 24, dp(50), lambda: self._rinvii(-1)))
        self.lbl_rinvii = etichetta("3", 24, True, altezza=dp(50))
        self.lbl_rinvii.size_hint_x = None
        self.lbl_rinvii.width = dp(44)
        r.add_widget(self.lbl_rinvii)
        r.add_widget(bottone("+", GRIGIO, 24, dp(50), lambda: self._rinvii(1)))
        box.add_widget(r)

        # --- SMS ---
        self._titolo(box, "SMS")
        self._interruttore(box, "sms_attivo", "Manda gli avvisi per SMS")
        box.add_widget(etichetta("Numeri di telefono (uno per riga)", 13, halign="left",
                                 altezza=dp(20)))
        self.numeri = TextInput(font_size=sp(18), size_hint_y=None, height=dp(84),
                                input_filter=lambda t, _: "".join(
                                    c for c in t if c.isdigit() or c in "+ ,;\n"))
        box.add_widget(self.numeri)

        # --- Telegram ---
        self._titolo(box, "TELEGRAM (gratis, serve internet)")
        self._interruttore(box, "telegram_attivo", "Manda gli avvisi su Telegram")
        box.add_widget(etichetta("Codice del bot (lo dà @BotFather)", 13, halign="left",
                                 altezza=dp(20)))
        self.token = TextInput(font_size=sp(14), size_hint_y=None, height=dp(62),
                               hint_text="es. 123456789:AAH...")
        box.add_widget(self.token)
        box.add_widget(etichetta("Familiari collegati", 13, halign="left", altezza=dp(20)))
        self.lbl_chat = etichetta("", 15, halign="left")
        box.add_widget(self.lbl_chat)
        box.add_widget(etichetta("Ogni familiare apre il bot su Telegram e preme AVVIA,\n"
                                 "poi qui si tocca «Cerca familiari».", 12, colore=GRIGIO))
        r = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(8))
        r.add_widget(bottone("Cerca familiari", BLU, 16, dp(50), self.cerca))
        r.add_widget(bottone("Svuota elenco", GRIGIO, 15, dp(50), self.svuota_chat))
        box.add_widget(r)

        # --- azioni ---
        box.add_widget(etichetta("", 4, altezza=dp(6)))
        r = BoxLayout(size_hint_y=None, height=dp(56), spacing=dp(8))
        r.add_widget(bottone("SALVA", VERDE, 19, dp(56), self.salva))
        r.add_widget(bottone("Messaggio di prova", ARANCIO, 15, dp(56), self.prova))
        box.add_widget(r)
        box.add_widget(bottone("Indietro", GRIGIO, 17, dp(50),
                               lambda: App.get_running_app().vai("gestione")))

        box.add_widget(etichetta("Ultimi avvisi", 14, True, halign="left", altezza=dp(26)))
        self.ultimi = etichetta("", 12, colore=GRIGIO, halign="left")
        box.add_widget(self.ultimi)

        scroll.add_widget(box)
        esterno.add_widget(scroll)
        self.add_widget(esterno)

    def on_pre_enter(self, *_):
        app = App.get_running_app()
        self.dati = json.loads(json.dumps(app.avvisi))
        self.numeri.text = "\n".join(self.dati["numeri"])
        self.token.text = self.dati.get("token", "")
        self._aggiorna()

    def _aggiorna(self):
        for k, b in self.interruttori.items():
            acceso = self.dati.get(k)
            b.text = "SÌ" if acceso else "NO"
            b.background_color = hexc(VERDE if acceso else "#B0B0B0")
        self.lbl_rinvii.text = str(self.dati.get("rimandi_max", 3))
        chat = self.dati.get("chat", [])
        self.lbl_chat.text = ("\n".join(f"• {c['nome']}" for c in chat) if chat
                              else "nessuno")
        self.ultimi.text = App.get_running_app().ultimi_avvisi() or "Nessun avviso inviato."

    def _cambia(self, k):
        self.dati[k] = not self.dati.get(k)
        self._aggiorna()

    def _rinvii(self, delta):
        self.dati["rimandi_max"] = max(2, min(9, self.dati.get("rimandi_max", 3) + delta))
        self._aggiorna()

    def _leggi_campi(self):
        self.dati["numeri"] = pulisci_numeri(self.numeri.text)
        self.numeri.text = "\n".join(self.dati["numeri"])
        self.dati["token"] = "".join(self.token.text.split())
        self.token.text = self.dati["token"]

    def cerca(self):
        self._leggi_campi()
        if not self.dati["token"]:
            avviso("Attenzione", "Scrivi prima il codice del bot.")
            return
        self.lbl_chat.text = "Cerco…"

        def lavoro():
            trovate, errore = cerca_chat_telegram(self.dati["token"])
            Clock.schedule_once(lambda _: self._chat_trovate(trovate, errore))
        threading.Thread(target=lavoro, daemon=True).start()

    def _chat_trovate(self, trovate, errore):
        if errore:
            self._aggiorna()
            avviso("Telegram", f"Non riesco a collegarmi:\n{errore}")
            return
        gia = {c["id"] for c in self.dati.get("chat", [])}
        nuove = [c for c in trovate if c["id"] not in gia]
        self.dati["chat"] = self.dati.get("chat", []) + nuove
        self._aggiorna()
        if nuove:
            avviso("Telegram", "Trovati:\n" + "\n".join(c["nome"] for c in nuove) +
                   "\n\nRicorda di premere SALVA.")
        else:
            avviso("Telegram", "Nessun nuovo familiare.\nChiedi di aprire il bot su Telegram,\n"
                               "premere AVVIA e poi riprova.")

    def svuota_chat(self):
        self.dati["chat"] = []
        self._aggiorna()

    def salva(self):
        self._leggi_campi()
        app = App.get_running_app()
        app.avvisi = json.loads(json.dumps(self.dati))
        app.salva_avvisi()
        if self.dati.get("sms_attivo") and self.dati["numeri"]:
            chiedi_permesso_sms()
        canali = []
        if self.dati.get("sms_attivo") and self.dati["numeri"]:
            canali.append(f"SMS a {len(self.dati['numeri'])} numeri")
        if self.dati.get("telegram_attivo") and self.dati["token"] and self.dati.get("chat"):
            canali.append(f"Telegram a {len(self.dati['chat'])} familiari")
        avviso("Salvato", "Avvisi salvati.\n" + ("\n".join(canali) if canali else
                                                  "Attenzione: nessun canale attivo,\n"
                                                  "non partirà nessun avviso."))

    def prova(self):
        self._leggi_campi()
        app = App.get_running_app()
        n = app.manda_avviso("Promemoria Medicine: questo è un messaggio di prova. "
                             "Gli avvisi funzionano.", self.dati)
        if n == 0:
            avviso("Prova", "Nessun canale attivo con destinatari:\n"
                            "metti SÌ su SMS o Telegram e aggiungi i destinatari.")
        else:
            avviso("Prova", f"Inviati {n} messaggi.\nTra qualche secondo l'esito\n"
                            f"compare in «Ultimi avvisi».")
        Clock.schedule_once(lambda _: self._aggiorna(), 6)
        self._aggiorna()


# =====================================================
#  CONFIGURAZIONE GUIDATA (al primo avvio, o da Impostazioni)
# =====================================================
class SchermataGuida(Screen):
    """Una domanda per schermata, con pulsanti grandi. Le scelte vengono salvate subito."""

    def __init__(self, **kw):
        super().__init__(**kw)
        esterno = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(8))
        sfondo(esterno, BG)
        self.lbl_passo = etichetta("", 14, colore=GRIGIO)
        esterno.add_widget(self.lbl_passo)
        self.lbl_titolo = etichetta("", 26, True)
        esterno.add_widget(self.lbl_titolo)
        scroll = ScrollView()
        self.corpo = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(10),
                               padding=(0, dp(6)))
        self.corpo.bind(minimum_height=self.corpo.setter("height"))
        scroll.add_widget(self.corpo)
        esterno.add_widget(scroll)
        self.btn_indietro = bottone("< Indietro", GRIGIO, 16, dp(46), self.indietro)
        esterno.add_widget(self.btn_indietro)
        self.add_widget(esterno)
        self.storia = []
        self.passo = "benvenuto"
        self.vuole_avvisi = False
        self.ev_prova = None

    # ---- navigazione ----
    PASSI = ["benvenuto", "telefono", "apertura", "batteria", "esempi", "persone",
             "suono", "avvisi", "fine"]

    def ricomincia(self):
        self.storia = []
        self.vai("benvenuto", ricorda=False)

    def vai(self, passo, ricorda=True):
        self._ferma_prova()
        if ricorda:
            self.storia.append(self.passo)
        self.passo = passo
        self.disegna()

    def indietro(self):
        self._ferma_prova()
        if self.storia:
            self.passo = self.storia.pop()
            self.disegna()

    def on_enter(self, *_):
        self.disegna()      # aggiorna lo stato dopo il ritorno da un'impostazione di Android

    def disegna(self):
        self.corpo.clear_widgets()
        n = self.PASSI.index(self.passo)
        self.lbl_passo.text = f"Configurazione guidata · passo {n + 1} di {len(self.PASSI)}"
        self.btn_indietro.opacity = 1 if self.storia else 0
        self.btn_indietro.disabled = not self.storia
        getattr(self, "p_" + self.passo)()

    # ---- piccoli aiuti ----
    def _testo(self, t, size=18, colore=TESTO, bold=False):
        self.corpo.add_widget(etichetta(t, size, bold, colore))

    def _scelta(self, testo, colore, azione, alto=dp(72), size=20):
        self.corpo.add_widget(bottone(testo, colore, size, alto, azione))

    def _stato(self, ok, testo_ok, testo_no):
        l = Label(text=("OK - " + testo_ok) if ok else ("DA FARE - " + testo_no),
                  font_size=sp(17), bold=True, color=(1, 1, 1, 1), size_hint_y=None,
                  height=dp(48))
        sfondo(l, VERDE if ok else ROSSO)
        self.corpo.add_widget(l)

    def _salva(self, **valori):
        app = App.get_running_app()
        app.avvisi.update(valori)
        app.salva_avvisi()

    # ---- i passi ----
    def p_benvenuto(self):
        self.lbl_titolo.text = "Benvenuto!"
        self._testo("Ti faccio qualche domanda per preparare l'app nel modo giusto.\n\n"
                    "Ci vogliono 2 minuti. Ogni scelta si può cambiare dopo da "
                    "Impostazioni > Configurazione guidata.")
        self._scelta("INIZIAMO", VERDE, lambda: self.vai("telefono"), dp(80), 24)

    def p_telefono(self):
        self.lbl_titolo.text = "Su quale telefono è installata?"
        self._testo("È il telefono che la persona usa tutti i giorni\n(per chiamare, WhatsApp…)?", 18)
        self._scelta("SÌ, è il suo telefono personale\n[size=15]l'app si apre da sola solo all'ora "
                     "delle medicine[/size]", BLU, lambda: self._telefono(True), dp(92))
        self._scelta("NO, è un telefono dedicato\n[size=15]sempre in carica, mostra l'orologio "
                     "tutto il giorno[/size]", ARANCIO, lambda: self._telefono(False), dp(92))
        for b in self.corpo.children[:2]:
            b.markup = True

    def _telefono(self, personale):
        self._salva(telefono_personale=personale, schermo_sempre_acceso=not personale)
        App.get_running_app().applica_schermo()
        self.vai("apertura")

    def p_apertura(self):
        app = App.get_running_app()
        personale = app.avvisi.get("telefono_personale")
        ok = puo_aprirsi_da_sola()
        self.lbl_titolo.text = "Aprirsi da sola all'ora giusta"
        self._testo("Per suonare anche quando l'app è chiusa e lo schermo è spento, "
                    "Android deve permetterle di «comparire sopra le altre app».", 17)
        self._stato(ok, "permesso concesso", "permesso da concedere")
        if not ok:
            self._testo("Tocca il pulsante qui sotto: si apre un'impostazione di Android. "
                        "Attiva «Promemoria Medicine», poi torna indietro.", 15, GRIGIO)
            self._scelta("CONCEDI IL PERMESSO", VERDE, chiedi_apertura_da_sola)
        if not personale:
            self._testo("\nTelefono dedicato: conviene usare l'app come SCHERMATA HOME. "
                        "Così riparte da sola quando si riaccende il telefono e il tasto Home "
                        "riporta sempre all'orologio.", 16)
            self._scelta("USA COME SCHERMATA HOME\n[size=14]scegli «Promemoria Medicine»[/size]",
                         BLU, lambda: apri_impostazioni_telefono("android.settings.HOME_SETTINGS"),
                         dp(80), 18)
            self.corpo.children[0].markup = True
        self._scelta("AVANTI  >", VERDE if ok else GRIGIO, lambda: self.vai("batteria"))

    def p_batteria(self):
        ok = batteria_senza_limiti()
        self.lbl_titolo.text = "Risparmio batteria"
        self._testo("Il risparmio batteria di Android potrebbe «addormentare» l'app e "
                    "farle saltare una sveglia. Va tolto per Promemoria Medicine.", 17)
        self._stato(ok, "nessun limite", "c'è un limite da togliere")
        if not ok:
            self._testo("Tocca il pulsante e rispondi «Consenti».", 15, GRIGIO)
            self._scelta("TOGLI IL LIMITE", VERDE, chiedi_batteria_senza_limiti)
        self._scelta("AVANTI  >", VERDE if ok else GRIGIO, self._dopo_batteria)

    def _dopo_batteria(self):
        esempi = [m for m in App.get_running_app().medicine if e_esempio(m)]
        self.vai("esempi" if esempi else "persone")

    def p_esempi(self):
        app = App.get_running_app()
        esempi = [m for m in app.medicine if e_esempio(m)]
        self.lbl_titolo.text = "Medicine di esempio"
        self._testo("Nell'app ci sono medicine inventate, messe solo per mostrare come funziona:", 17)
        self._testo("\n".join(f"• {m['nome']} ({', '.join(m['orari'])})" for m in esempi), 17,
                    bold=True)
        self._testo("Vuoi cancellarle?", 18)
        self._scelta("SÌ, CANCELLALE", VERDE, self._cancella_esempi)
        self._scelta("NO, tienile per fare prove", GRIGIO, lambda: self.vai("persone"), dp(60), 17)

    def _cancella_esempi(self):
        app = App.get_running_app()
        app.medicine = [m for m in app.medicine if not e_esempio(m)]
        app.salva()
        self.vai("persone")

    def p_persone(self):
        app = App.get_running_app()
        self.lbl_titolo.text = "Chi prende le medicine?"
        self._testo("Scrivi il nome di ogni persona (come la chiamate in casa: la voce "
                    "lo pronuncerà). Una persona va benissimo. Lascia vuoto per togliere.", 16)
        self.campi_persone = []
        for p in app.persone:
            self._campo_persona(p)
        for _ in range(max(1, 3 - len(app.persone))):
            self._campo_persona(None)
        self._scelta("AVANTI  >", VERDE, self._salva_persone)

    def _campo_persona(self, persona):
        forte = colori_di(persona)[0] if persona else "#B0B0B0"
        r = BoxLayout(size_hint_y=None, height=dp(56), spacing=dp(8))
        quadro = Label(size_hint_x=None, width=dp(16))
        sfondo(quadro, forte)
        r.add_widget(quadro)
        ti = TextInput(text=persona["nome"] if persona else "", multiline=False,
                       font_size=sp(22), hint_text="nome")
        r.add_widget(ti)
        self.corpo.add_widget(r)
        self.campi_persone.append((persona, ti))

    def _salva_persone(self):
        app = App.get_running_app()
        bloccate = []
        for persona, ti in self.campi_persone:
            nome = ti.text.strip()
            if persona and nome:
                persona["nome"] = nome
            elif persona and not nome:
                if any(m.get("persona") == persona["id"] for m in app.medicine):
                    bloccate.append(persona["nome"])
                else:
                    app.persone.remove(persona)
            elif nome:
                usati = {p.get("colore") for p in app.persone}
                liberi = [c for c in PALETTE if c not in usati] or list(PALETTE)
                app.persone.append({"id": uuid.uuid4().hex[:8], "nome": nome, "colore": liberi[0]})
        if not app.persone:
            avviso("Attenzione", "Scrivi almeno un nome.")
            self.disegna()
            return
        app.salva_persone()
        if bloccate:
            avviso("Non tolte", "Non ho tolto:\n" + "\n".join(bloccate) +
                   "\nperché hanno ancora delle medicine.")
        self.vai("suono")

    def p_suono(self):
        app = App.get_running_app()
        self.lbl_titolo.text = "Prova del suono"
        self._testo("Tocca il pulsante: per qualche secondo suona la suoneria, poi parla la voce. "
                    "Usa il volume SVEGLIA, quindi si sente anche in silenzioso.", 17)
        nome = app.persone[0]["nome"] if app.persone else ""
        self._scelta("PROVA IL SUONO", ARANCIO, lambda: self._prova(nome), dp(80), 22)
        self._testo("Se la voce non è chiara: Impostazioni del telefono > Gestione generale > "
                    "Sintesi vocale > scegli il motore di Google e la voce italiana.", 14, GRIGIO)
        self._scelta("SI SENTE BENE  >", VERDE, lambda: self.vai("avvisi"))

    def _prova(self, nome):
        voce = App.get_running_app().voce
        self._ferma_prova()
        voce.avvia_suoneria(0.7)

        def parla(_):
            voce.ferma_suoneria()
            voce.parla(f"{nome}, questa è una prova. È ora di prendere la medicina."
                       if nome else "Questa è una prova. È ora di prendere la medicina.")
        self.ev_prova = Clock.schedule_once(parla, 5)

    def _ferma_prova(self):
        if self.ev_prova:
            self.ev_prova.cancel()
            self.ev_prova = None
            voce = App.get_running_app().voce
            voce.ferma_suoneria()
            voce.zitto()

    def p_avvisi(self):
        self.lbl_titolo.text = "Avvisare un familiare?"
        self._testo("L'app può mandare un SMS o un messaggio Telegram a un figlio o a chi "
                    "assiste, per esempio se una medicina non viene confermata.", 17)
        self._scelta("SÌ, li imposto alla fine", VERDE, lambda: self._avvisi(True))
        self._scelta("NO, per ora no", GRIGIO, lambda: self._avvisi(False), dp(60), 18)

    def _avvisi(self, si):
        self.vuole_avvisi = si
        self.vai("fine")

    def p_fine(self):
        app = App.get_running_app()
        self._salva(configurato=True)
        self.lbl_titolo.text = "Fatto!"
        personale = app.avvisi.get("telefono_personale")
        righe = [
            "• Telefono: " + ("personale" if personale else "dedicato"),
            "• Apertura automatica: " + ("OK" if puo_aprirsi_da_sola() else "DA ATTIVARE"),
            "• Batteria: " + ("OK" if batteria_senza_limiti() else "DA SISTEMARE"),
            "• Persone: " + ", ".join(p["nome"] for p in app.persone),
            f"• Medicine inserite: {len(app.medicine)}",
        ]
        self._testo("\n".join(righe), 17)
        self._testo("Le impostazioni si aprono dall'orologio tenendo premuto "
                    "il pulsante in basso per 2 secondi.", 15, GRIGIO)
        self._scelta("AGGIUNGI UNA MEDICINA", VERDE, lambda: self._esci("medicina"))
        if self.vuole_avvisi:
            self._scelta("IMPOSTA GLI AVVISI AI FAMILIARI", "#00897B", lambda: self._esci("avvisi"),
                         dp(64), 18)
        self._scelta("VAI ALL'OROLOGIO", GRIGIO, lambda: self._esci("principale"), dp(60), 18)

    def _esci(self, dove):
        app = App.get_running_app()
        self._ferma_prova()
        if dove == "medicina":
            app.modifica(None)
        else:
            app.vai(dove)
        app.programma_prossima(forza=True)


# =====================================================
#  IMPOSTAZIONI: STORICO
# =====================================================
class SchermataStorico(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        sfondo(box, BG)
        box.add_widget(etichetta("Storico (ultimi 100)", 26, True))
        scroll = ScrollView()
        self.testo = etichetta("", 17, halign="left")
        self.testo.markup = True
        scroll.add_widget(self.testo)
        box.add_widget(scroll)
        box.add_widget(bottone("Indietro", GRIGIO,
                               azione=lambda: App.get_running_app().vai("gestione")))
        self.add_widget(box)

    def on_pre_enter(self, *_):
        app = App.get_running_app()
        colori = {"presa": VERDE, "rimandata": ARANCIO, "non confermata": ROSSO}
        righe = []
        for r in reversed(leggi_registro(app.file_reg)[-100:]):
            try:
                giorno = dt.date.fromisoformat(r["data"]).strftime("%d/%m")
            except ValueError:
                giorno = r["data"]
            col = colori.get(r["esito"], GRIGIO).lstrip("#")
            chi = f"[b]{r['persona']}[/b] - " if r.get("persona") else ""
            righe.append(f"{giorno}  ore {r['ora_prevista']}  {chi}{r['medicina']}\n"
                         f"      [color={col}][b]{r['esito']}[/b][/color] alle {r['ora_risposta']}")
        self.testo.text = "\n".join(righe) if righe else "Nessuna registrazione."


# =====================================================
#  APPLICAZIONE
# =====================================================
class PromemoriaApp(App):
    title = "Promemoria Medicine"

    def build(self):
        Window.clearcolor = hexc(BG)
        if not ANDROID:
            Window.size = (420, 860)

        cartella = self.user_data_dir
        self.file_med = os.path.join(cartella, "medicine.json")
        self.file_reg = os.path.join(cartella, "registro.csv")
        self.file_persone = os.path.join(cartella, "persone.json")
        self.file_avvisi = os.path.join(cartella, "avvisi.json")
        self.file_sms = os.path.join(cartella, "avvisi_inviati.txt")
        self.lock_log = threading.Lock()
        self._risveglio = None
        self.avvisi = carica_avvisi(self.file_avvisi)
        self.conta_rinvii = {}
        self.cartella_media = os.path.join(cartella, "media")
        os.makedirs(self.cartella_media, exist_ok=True)

        self.voce = Voce(cartella)
        self.persone = carica_persone(self.file_persone)
        self.medicine = carica_medicine(self.file_med)
        # medicine delle versioni precedenti, senza persona: vanno alla prima persona
        if self.persone and any("persona" not in m for m in self.medicine):
            for m in self.medicine:
                m.setdefault("persona", self.persone[0]["id"])
            salva_medicine(self.file_med, self.medicine)
        aggiorna_registro_vecchio(self.file_reg)
        self.stato = stato_di_oggi(self.file_reg)
        self.rimandi = {}
        self.allarme = None

        self.sm = ScreenManager(transition=NoTransition())
        self.principale = SchermataPrincipale(name="principale")
        self.modifica_scr = SchermataModifica(name="modifica")
        self.sm.add_widget(self.principale)
        self.sm.add_widget(SchermataGestione(name="gestione"))
        self.sm.add_widget(self.modifica_scr)
        self.sm.add_widget(SchermataStorico(name="storico"))
        self.sm.add_widget(SchermataAvvisi(name="avvisi"))
        self.persona_scr = SchermataPersona(name="persona")
        self.sm.add_widget(SchermataPersone(name="persone"))
        self.sm.add_widget(self.persona_scr)
        self.guida = SchermataGuida(name="guida")
        self.sm.add_widget(self.guida)
        if not self.avvisi.get("configurato"):
            Clock.schedule_once(lambda _: self.avvia_guida(), 0.5)

        self.principale.aggiorna_lista()
        Clock.schedule_once(lambda _: self.controlla(), 2)
        Clock.schedule_interval(lambda _: self.controlla(), 15)
        self.applica_schermo()
        self.programma_prossima()
        return self.sm

    def on_pause(self):
        self.programma_prossima(forza=True)
        return True

    def on_resume(self):
        self.applica_schermo()
        if self.sm.current == "gestione":
            self.sm.get_screen("gestione")._testo_schermo()
        elif self.sm.current == "guida":
            Clock.schedule_once(lambda _: self.guida.disegna(), 0.3)
        Clock.schedule_once(lambda _: self.controlla(), 0.5)   # se ci ha svegliato la sveglia

    def applica_schermo(self):
        dedicato = not self.avvisi.get("telefono_personale", False)
        imposta_schermo(bool(self.allarme) or dedicato)

    def avvia_guida(self):
        self.guida.ricomincia()
        self.sm.current = "guida"

    def dopo_la_risposta(self):
        """Sul telefono personale l'app torna in secondo piano dopo la risposta
        (a meno che non stia già suonando un'altra medicina)."""
        if self.avvisi.get("telefono_personale") and self.allarme is None:
            metti_in_secondo_piano()

    # ---- sveglia di sistema: riaccende il telefono anche in standby ----
    def prossima_sveglia(self):
        adesso = dt.datetime.now()
        candidati = []
        for m in self.medicine:
            for o in m["orari"]:
                h, mi = map(int, o.split(":"))
                for giorni in (0, 1):
                    t = (adesso + dt.timedelta(days=giorni)).replace(
                        hour=h, minute=mi, second=0, microsecond=0)
                    k = (m["id"], o, t.date().isoformat())
                    if self.stato.get(k) in ("presa", "non confermata"):
                        continue
                    if k in self.rimandi:
                        t = max(t, self.rimandi[k])
                    if t > adesso:
                        candidati.append(t)
                        break
        return min(candidati) if candidati else None

    def programma_prossima(self, forza=False):
        t = self.prossima_sveglia()
        if t is None or (t == self._risveglio and not forza):
            return
        if programma_risveglio(int(t.timestamp() * 1000)):
            self._risveglio = t

    # ---- navigazione ----
    def vai(self, nome):
        if nome == "principale":
            self.principale.aggiorna_lista()
        self.sm.current = nome

    def modifica(self, indice):
        self.modifica_scr.carica(indice)
        self.sm.current = "modifica"

    def salva(self):
        salva_medicine(self.file_med, self.medicine)
        self.principale.aggiorna_lista()
        self.programma_prossima()

    # ---- avvisi ai familiari (SMS e Telegram) ----
    def salva_avvisi(self):
        salva_medicine(self.file_avvisi, self.avvisi)

    def _scrivi_log(self, canale, dest, ok, motivo, testo):
        riga = (f"{dt.datetime.now():%d/%m %H:%M}  {canale} {dest}  "
                f"{'inviato' if ok else 'NON inviato: ' + motivo}  |  {testo}")
        with self.lock_log:
            try:
                with open(self.file_sms, "a", encoding="utf-8") as f:
                    f.write(riga + "\n")
            except Exception:
                pass

    def manda_sms(self, numero, testo):
        ok, motivo = invia_sms(numero, testo)
        self._scrivi_log("SMS", numero, ok, motivo, testo)
        return ok

    def manda_telegram(self, token, chat, testo):
        """In un thread separato: la rete può essere lenta e l'app non deve bloccarsi."""
        def lavoro():
            ok, motivo = invia_telegram(token, chat["id"], testo)
            self._scrivi_log("Telegram", chat["nome"], ok, motivo, testo)
        threading.Thread(target=lavoro, daemon=True).start()

    def manda_avviso(self, testo, impostazioni=None):
        """Manda il testo su tutti i canali attivi. Restituisce quanti messaggi sono partiti."""
        a = impostazioni or self.avvisi
        n = 0
        if a.get("sms_attivo"):
            for numero in a.get("numeri", []):
                self.manda_sms(numero, testo)
                n += 1
        if a.get("telegram_attivo") and a.get("token"):
            for chat in a.get("chat", []):
                self.manda_telegram(a["token"], chat, testo)
                n += 1
        return n

    def ultimi_avvisi(self, quanti=8):
        if not os.path.exists(self.file_sms):
            return ""
        with self.lock_log:
            with open(self.file_sms, encoding="utf-8") as f:
                righe = f.read().strip().splitlines()
        return "\n".join(reversed(righe[-quanti:]))

    def avvisa_familiari(self, tipo, med, orario, extra=""):
        if not self.avvisi.get(tipo):
            return
        nome = self._nome_persona(med) or "La persona"
        ora = dt.datetime.now().strftime("%H:%M")
        if tipo == "presa":
            testo = f"{nome} ha preso {med['nome']} (dose delle {orario}) alle {ora}."
        elif tipo == "non_confermata":
            testo = (f"ATTENZIONE: {nome} NON ha confermato {med['nome']} delle {orario}. "
                     f"Nessuna risposta in {TIMEOUT_MIN} minuti.")
        else:
            testo = (f"ATTENZIONE: {nome} ha rimandato {extra} volte {med['nome']} "
                     f"delle {orario} (ultimo rinvio alle {ora}).")
        self.manda_avviso("Promemoria Medicine - " + testo)

    def salva_persone(self):
        salva_medicine(self.file_persone, self.persone)
        self.principale.aggiorna_lista()

    def modifica_persona(self, indice):
        self.persona_scr.carica(indice)
        self.sm.current = "persona"

    def persona_di(self, med):
        for p in self.persone:
            if p["id"] == med.get("persona"):
                return p
        return None

    def _nome_persona(self, med):
        p = self.persona_di(med)
        return p["nome"] if p else ""

    def copia_in_app(self, percorso):
        """Copia foto/audio dentro l'app, così restano anche se l'originale viene cancellato."""
        est = os.path.splitext(percorso)[1].lower()
        if est == ".jfif":          # è un normale jpg salvato dal browser
            est = ".jpg"
        dest = os.path.join(self.cartella_media, uuid.uuid4().hex + est)
        try:
            shutil.copy(percorso, dest)
            return dest
        except Exception as e:
            avviso("Errore", f"Non riesco a leggere il file:\n{e}")
            return ""

    # ---- controllo orari ----
    def controlla(self):
        if self.allarme is None:
            adesso = dt.datetime.now()
            oggi = adesso.date().isoformat()
            for m in self.medicine:
                for orario in m["orari"]:
                    k = (m["id"], orario, oggi)
                    if self.stato.get(k) in ("presa", "non confermata"):
                        continue
                    h, mi = map(int, orario.split(":"))
                    previsto = adesso.replace(hour=h, minute=mi, second=0, microsecond=0)
                    if adesso < previsto:
                        continue
                    if k in self.rimandi:
                        if adesso < self.rimandi[k]:
                            continue
                    elif adesso - previsto > dt.timedelta(hours=MAX_RITARDO_ORE):
                        continue
                    self.apri_allarme(m, orario, k)
                    break
                if self.allarme:
                    break
        if self.sm.current == "principale":
            self.principale.aggiorna_lista()

    def apri_allarme(self, med, orario, chiave):
        if self.allarme:
            return
        self.allarme = Allarme(self, med, orario, chiave)
        self.allarme.open()
        self.applica_schermo()

    # ---- risposte ----
    def esito(self, chiave, med, orario, esito):
        self.allarme = None
        self.applica_schermo()
        self.rimandi.pop(chiave, None)
        if orario == "PROVA":
            if esito == "presa":
                self.voce.parla("Prova riuscita.")
            return
        self.stato[chiave] = esito
        self.conta_rinvii.pop(chiave, None)
        nome = self._nome_persona(med)
        registra(self.file_reg, med, orario, esito, nome)
        self.avvisa_familiari(esito.replace(" ", "_"), med, orario)
        self.programma_prossima()
        if esito == "presa":
            self.voce.parla(f"Grazie {nome}!" if nome else "Grazie!")
        Clock.schedule_once(lambda _: self.dopo_la_risposta(), 6)
        self.principale.aggiorna_lista()
        Clock.schedule_once(lambda _: self.controlla(), 3)   # se c'è un'altra medicina in attesa

    def rimanda(self, chiave, med, orario):
        self.allarme = None
        self.applica_schermo()
        if orario == "PROVA":
            return
        self.rimandi[chiave] = dt.datetime.now() + dt.timedelta(minutes=RIMANDA_MIN)
        registra(self.file_reg, med, orario, "rimandata", self._nome_persona(med))
        n = self.conta_rinvii.get(chiave, 0) + 1
        self.conta_rinvii[chiave] = n
        if n == self.avvisi.get("rimandi_max", 3):
            self.avvisa_familiari("rimandi", med, orario, n)
        self.programma_prossima()
        self.voce.parla(f"Va bene, te lo ricordo tra {RIMANDA_MIN} minuti.")
        Clock.schedule_once(lambda _: self.dopo_la_risposta(), 6)
        self.principale.aggiorna_lista()


if __name__ == "__main__":
    PromemoriaApp().run()

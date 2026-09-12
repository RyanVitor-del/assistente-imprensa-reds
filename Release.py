import os
import time
import re
import tkinter as tk
from tkinter import messagebox, ttk, filedialog, simpledialog
import threading
from PyPDF2 import PdfReader
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
import google.generativeai as genai
from PIL import Image, ImageTk
import sys
import fitz  # PyMuPDF
import cloudinary
import cloudinary.uploader
import requests
import json
import csv
import queue
import platform
import subprocess
from datetime import datetime, timedelta


class CancelledError(Exception):
    """Sinaliza que o usuário solicitou cancelamento."""
    pass


def diretorio_aplicativo():
    """Diretório gravável ao lado do script/executável, sem depender do CWD."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = diretorio_aplicativo()
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
CONFIG_MUNICIPIO_PATH = os.path.join(APP_DIR, "configmunicipio.json")
CONFIG_UNIDADE_PATH = os.path.join(APP_DIR, "configunidade.json")
def carregar_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def carregar_municipios():
    if os.path.exists(CONFIG_MUNICIPIO_PATH):
        with open(CONFIG_MUNICIPIO_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "municipios_salvos": [],
        "ultimo_usado": ""
    }

def salvar_municipios(dados):
    with open(CONFIG_MUNICIPIO_PATH, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=4)


def carregar_unidades():
    """
    Estrutura do arquivo (configunidade.json):
    {
      "unidades_por_municipio": { "FORMIGA": ["1ª CIA", "2ª CIA"] },
      "ultimo_por_municipio": { "FORMIGA": "1ª CIA" }
    }
    """
    if os.path.exists(CONFIG_UNIDADE_PATH):
        try:
            with open(CONFIG_UNIDADE_PATH, "r", encoding="utf-8") as f:
                dados = json.load(f) or {}
            # Normaliza chaves
            dados.setdefault("unidades_por_municipio", {})
            dados.setdefault("ultimo_por_municipio", {})
            return dados
        except Exception:
            pass

    return {"unidades_por_municipio": {}, "ultimo_por_municipio": {}}


def salvar_unidades(dados):
    with open(CONFIG_UNIDADE_PATH, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=4, ensure_ascii=False)


def salvar_config(dados):
    # Carrega o que já existe no arquivo
    config_atual = {}

    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config_atual = json.load(f)
        except:
            config_atual = {}

    # Atualiza apenas as chaves enviadas
    config_atual.update(dados)

    # Salva mantendo o restante
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config_atual, f, indent=4, ensure_ascii=False)

# ===============================
# 📸 Instagram Token (validação/expiração)
# ===============================
def _parse_iso_dt(val: str):
    try:
        return datetime.fromisoformat(val)
    except Exception:
        return None

def verificar_token_instagram(token: str, timeout: int = 8):
    """Verifica se o token do Instagram/Facebook Graph está válido e tenta obter expiração.

    Retorna dict:
      - is_valid: True/False/None
      - expires_at: datetime|None
      - source: 'debug_token' | 'me' | 'config' | 'guess' | 'error'
      - error: str|None
    """
    token = (token or "").strip()
    if not token:
        return {"is_valid": False, "expires_at": None, "source": "error", "error": "Token não configurado."}

    # 1) Tenta debug_token (melhor cenário: vem expires_at)
    try:
        r = requests.get(
            "https://graph.facebook.com/debug_token",
            params={"input_token": token, "access_token": token},
            timeout=timeout
        )
        j = r.json() if r is not None else {}
        data = j.get("data") if isinstance(j, dict) else None
        if isinstance(data, dict):
            is_valid = data.get("is_valid")
            exp = data.get("expires_at")
            expires_at = None
            if isinstance(exp, (int, float)) and exp > 0:
                expires_at = datetime.fromtimestamp(int(exp))
            return {"is_valid": bool(is_valid), "expires_at": expires_at, "source": "debug_token", "error": None}
    except Exception:
        pass

    # 2) Fallback: /me (valida, mas normalmente não informa expiração)
    try:
        r = requests.get(
            "https://graph.facebook.com/v19.0/me",
            params={"fields": "id", "access_token": token},
            timeout=timeout
        )
        j = r.json() if r is not None else {}
        if isinstance(j, dict) and j.get("id"):
            return {"is_valid": True, "expires_at": None, "source": "me", "error": None}
        msg = ""
        if isinstance(j, dict):
            msg = (j.get("error") or {}).get("message", "") or str(j)
        return {"is_valid": False, "expires_at": None, "source": "me", "error": msg}
    except Exception as e:
        return {"is_valid": None, "expires_at": None, "source": "error", "error": str(e)}

def estimar_expiracao_token(cfg: dict):
    """Obtém expiração do token a partir da config (se existir) ou faz estimativa.

    - Se houver insta_token_expires_at (ISO), usa.
    - Senão, se houver insta_token_saved_at (ISO), estima +60 dias (padrão de token longo).
    """
    if not isinstance(cfg, dict):
        return None, "error"
    exp_iso = (cfg.get("insta_token_expires_at") or "").strip()
    if exp_iso:
        dt = _parse_iso_dt(exp_iso)
        if dt:
            return dt, (cfg.get("insta_token_expires_source") or "config")
    saved_iso = (cfg.get("insta_token_saved_at") or "").strip()
    dt_saved = _parse_iso_dt(saved_iso) if saved_iso else None
    if dt_saved:
        return (dt_saved + timedelta(days=60)), "guess"
    return None, "unknown"

# --- CONFIGURAÇÃO CLOUDINARY ---

def recurso_caminho(relativo):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relativo)
    return os.path.join(APP_DIR, "assets", relativo)


def abrir_pasta(caminho: str):
    """Abre uma pasta no explorador de arquivos (Windows/macOS/Linux)."""
    try:
        caminho = os.path.abspath(caminho)
        if os.name == "nt":
            os.startfile(caminho)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", caminho])
        else:
            subprocess.Popen(["xdg-open", caminho])
    except Exception as e:
        try:
            messagebox.showerror("Erro", f"Não foi possível abrir a pasta:\n{e}")
        except Exception:
            print("Não foi possível abrir a pasta:", e)


# BIBLIOTECA PARA O CALENDÁRIO
try:
    from tkcalendar import DateEntry
except ImportError:
    DateEntry = None
    print("❌ Erro: Biblioteca 'tkcalendar' não encontrada. Instale com: pip install tkcalendar")

class SISPBotImprensaCompleto:
    REDS_HOME_URL = "https://web.sids.mg.gov.br/reds/index.do"
    REDS_ADVANCED_SEARCH_URL = (
        "https://web.sids.mg.gov.br/reds/consultas/"
        "consultaAvancada.do?operation=loadForSearch&tela=CA"
    )

    def __init__(self, log_widget=None):
        cfg = carregar_config()
        self.log_widget = log_widget
        self.driver = None
        self.wait = None
        self.last_login_at = None
        genai.configure(api_key=cfg.get("gemini_key"))
        cloudinary.config(
            cloud_name=cfg.get("cloud_name"),
            api_key=cfg.get("cloud_key"),
            api_secret=cfg.get("cloud_secret"),
            secure=True
        )

        try:
            self.model = genai.GenerativeModel('gemini-2.5-flash')
            self.escrever_log("Iniciando...")
        except Exception:
            try:
                self.model = genai.GenerativeModel('gemini-3-flash-preview')
                self.escrever_log("Iniciando...")
            except Exception as e:
                self.escrever_log(f"Erro ao carregar modelos atuais: {e}")

        self.download_path = os.path.join(diretorio_aplicativo(), "temp_pdf")
        if not os.path.exists(self.download_path):
            os.makedirs(self.download_path)

        self._iniciar_navegador()

    def _opcoes_firefox(self):
        options = webdriver.FirefoxOptions()
        options.add_argument("--headless")
        options.set_capability("acceptInsecureCerts", True)
        options.set_preference("browser.download.folderList", 2)
        options.set_preference("browser.download.dir", self.download_path)
        options.set_preference("browser.download.useDownloadDir", True)
        options.set_preference("browser.download.alwaysOpenPanel", False)
        options.set_preference("browser.helperApps.neverAsk.saveToDisk", "application/pdf")
        options.set_preference("pdfjs.disabled", True)
        options.set_preference("security.enterprise_roots.enabled", True)
        return options

    def _iniciar_navegador(self, tentativas=2):
        """Inicia o Firefox com uma repetição curta para falhas transitórias."""
        ultimo_erro = None
        self.driver = None
        self.wait = None

        for tentativa in range(1, tentativas + 1):
            driver = None
            try:
                driver = webdriver.Firefox(options=self._opcoes_firefox())
                driver.set_window_size(1920, 1080)
                self.driver = driver
                self.wait = WebDriverWait(driver, 30)
                return True
            except Exception as e:
                ultimo_erro = e
                try:
                    if driver:
                        driver.quit()
                except Exception:
                    pass
                if tentativa < tentativas:
                    self.escrever_log(
                        f"Firefox não iniciou na tentativa {tentativa}; tentando novamente..."
                    )
                    time.sleep(1)

        detalhe = str(ultimo_erro).strip() or type(ultimo_erro).__name__
        self.escrever_log(f"Erro ao iniciar Firefox/Geckodriver: {detalhe}")
        return False

    def navegador_ativo(self):
        if not self.driver:
            return False
        try:
            self.driver.execute_script("return 1")
            return True
        except Exception:
            return False

    def _garantir_navegador(self):
        if self.navegador_ativo():
            return
        if not self._iniciar_navegador():
            raise RuntimeError(
                "Não foi possível iniciar o Firefox. Feche instâncias travadas e tente novamente."
            )

    def _encontrar_primeiro(self, localizadores, timeout=30, clicavel=False):
        def localizar(driver):
            for by, seletor in localizadores:
                try:
                    for elemento in driver.find_elements(by, seletor):
                        if elemento.is_displayed() and (not clicavel or elemento.is_enabled()):
                            return elemento
                except Exception:
                    continue
            return False

        return WebDriverWait(self.driver, timeout).until(localizar)

    def _preencher_campo(self, elemento, valor):
        elemento.click()
        elemento.send_keys(Keys.CONTROL, "a")
        elemento.send_keys(Keys.BACKSPACE)
        elemento.send_keys(valor)

    def _pagina_de_login(self):
        if not self.driver:
            return False
        try:
            url = (self.driver.current_url or "").lower()
            if "authenticationendpoint/login" in url:
                return True
            return any(
                elemento.is_displayed()
                for elemento in self.driver.find_elements(By.ID, "usernameUserInput")
            )
        except Exception:
            return False

    def sessao_autenticada(self):
        if not self.navegador_ativo() or self._pagina_de_login():
            return False
        try:
            url = (self.driver.current_url or "").lower()
            if "web.sids.mg.gov.br/reds/" not in url:
                return False
            if self.driver.find_elements(By.XPATH, "//a[contains(@href,'operation=logout')]"):
                return True
            texto = self.driver.find_element(By.TAG_NAME, "body").text
            return "Usuário conectado:" in texto or "Bem-vindo ao sistema" in texto
        except Exception:
            return False

    def _mensagem_erro_login(self):
        localizadores = (
            (By.ID, "error-msg"),
            (By.CSS_SELECTOR, ".alert-danger"),
            (By.CSS_SELECTOR, "[role='alert']"),
            (By.CSS_SELECTOR, ".ui-messages-error-summary"),
        )
        for by, seletor in localizadores:
            try:
                for elemento in self.driver.find_elements(by, seletor):
                    texto = elemento.text.strip()
                    if elemento.is_displayed() and texto:
                        return texto
            except Exception:
                continue
        return ""

    def fechar_avisos(self):
        """Fecha avisos modais que o REDS exibe logo após o login."""
        xpath = (
            "//div[contains(concat(' ',normalize-space(@class),' '),' modal ') "
            "and not(@aria-hidden='true')]"
            "//*[self::a or self::button][normalize-space()='Fechar' "
            "or normalize-space()='×' or @aria-label='Close']"
        )
        fechados = 0
        for _ in range(4):
            botao = None
            try:
                for candidato in self.driver.find_elements(By.XPATH, xpath):
                    if candidato.is_displayed():
                        botao = candidato
                        break
            except Exception:
                break
            if not botao:
                break
            try:
                self.driver.execute_script("arguments[0].click();", botao)
                fechados += 1
                WebDriverWait(self.driver, 3).until(lambda _d: not botao.is_displayed())
            except Exception:
                break
        return fechados

    def esperar_pagina_carregar(self, timeout=30):
        WebDriverWait(self.driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

    def fazer_login_seguro(self, user, pw, tentativas=3, cancel_event=None):
        self._garantir_navegador()
        if self.sessao_autenticada():
            self.fechar_avisos()
            self.escrever_log("Sessão do REDS já autenticada.")
            return "sucesso"

        for tentativa in range(1, tentativas + 1):
            self._check_cancel(cancel_event)
            try:
                self.escrever_log(f"Tentativa de login {tentativa}...")
                self.esperar_pagina_carregar()

                campo_user = self._encontrar_primeiro(
                    (
                        (By.ID, "usernameUserInput"),
                        (By.NAME, "usernameUserInput"),
                        (By.CSS_SELECTOR, "input[placeholder='Usuário']"),
                    )
                )
                campo_pw = self._encontrar_primeiro(
                    (
                        (By.ID, "password"),
                        (By.NAME, "password"),
                        (By.CSS_SELECTOR, "input[type='password']"),
                    )
                )
                self._preencher_campo(campo_user, user)
                self._preencher_campo(campo_pw, pw)

                botao = self._encontrar_primeiro(
                    (
                        (By.CSS_SELECTOR, "[data-testid='login-page-continue-login-button']"),
                        (By.CSS_SELECTOR, "button[type='submit']"),
                        (
                            By.XPATH,
                            "//button[contains(.,'Entrar') or contains(.,'Acessar') or contains(.,'Continuar')]",
                        ),
                    ),
                    clicavel=True,
                )
                botao.click()

                resultado = WebDriverWait(self.driver, 35).until(
                    lambda _d: "sucesso"
                    if self.sessao_autenticada()
                    else ("erro", self._mensagem_erro_login())
                    if self._mensagem_erro_login()
                    else False
                )

                if resultado == "sucesso":
                    self.fechar_avisos()
                    self.escrever_log("Login realizado com sucesso!")
                    return "sucesso"

                if isinstance(resultado, tuple) and resultado[0] == "erro":
                    self.escrever_log("Login recusado pelo portal. Verifique usuário e senha.")
                    return "credencial_invalida"

            except CancelledError:
                raise

            except Exception as e:
                resumo = str(e).splitlines()[0].strip() or type(e).__name__
                self.escrever_log(f"⚠️ Erro técnico na tentativa {tentativa}: {resumo}")

            if tentativa < tentativas:
                self.escrever_log("Preparando uma nova tentativa de login...")
                if not self.navegador_ativo():
                    if not self._iniciar_navegador():
                        break
                try:
                    self.driver.get(self.REDS_HOME_URL)
                except Exception:
                    pass

        return "falha_tecnica"
    def escrever_log(self, texto):
        print(texto)
        if self.log_widget:
            self.log_widget.insert(tk.END, texto + "\n")
            self.log_widget.see(tk.END)

    def _check_cancel(self, cancel_event=None):
        """Levanta CancelledError se o usuário tiver solicitado cancelamento."""
        try:
            if cancel_event is not None and cancel_event.is_set():
                self.escrever_log("Operação cancelada pelo usuário.")
                raise CancelledError()
        except AttributeError:
            # cancel_event não é um Event ou não tem is_set
            return

    def esperar_loading(self):
        """Espera o indicador real do portal sem impor pausas fixas à consulta."""
        def carregando(driver):
            localizadores = (
                (By.XPATH, "//*[contains(normalize-space(text()),'Aguarde') and not(self::script)]"),
                (By.CSS_SELECTOR, ".loading, .loader, .blockUI, #loading"),
            )
            for by, seletor in localizadores:
                try:
                    if any(e.is_displayed() for e in driver.find_elements(by, seletor)):
                        return True
                except Exception:
                    continue
            return False

        try:
            WebDriverWait(self.driver, 2).until(carregando)
            self.escrever_log("Aguardando carregamento do sistema...")
            WebDriverWait(self.driver, 30).until(lambda d: not carregando(d))
        except TimeoutException:
            pass
        self.esperar_pagina_carregar(timeout=30)

    def formatar_texto_final(self, texto):
        if not texto: return ""
        texto = texto.replace("*", "").replace("#", "")
        texto = re.sub(r'^\d+\s+', '', texto)
        texto = re.sub(r'^[A-ZÇÁÉÍÓÚ\s]+/[A-Z]{2}\s*–\s*', '', texto)
        return " ".join(texto.split())
    def gerar_texto_com_pdf(self, caminho_pdf, tipo="release", cancel_event=None):
        self.escrever_log("Gerando texto...")

        try:
            self._check_cancel(cancel_event)
            arquivo = genai.upload_file(path=caminho_pdf)
            cfg = carregar_config()

            if tipo == "instagram":
                prompt = cfg.get("prompt_instagram", "")
            else:
                prompt = cfg.get("prompt_release", "")

            # Fallback caso esteja vazio
            if not prompt:
                if tipo == "instagram":
                    prompt = "Leia o PDF anexado e gere uma legenda profissional para Instagram."
                else:
                    prompt = "Leia o PDF anexado e gere uma release oficial completa."


            response = self.model.generate_content(
                [prompt, arquivo],
                generation_config={
                    "temperature": 0.8
                }
            )

            self._check_cancel(cancel_event)
            return response.text.strip()

        except CancelledError:
            raise

        except Exception as e:
            self.escrever_log(f"Erro na IA: {e}")
            return None



    def mostrar_lousa_release(self, texto_release, fotos):
        lousa = tk.Toplevel()
        lousa.title("Lousa de Release")
        lousa.geometry("900x750")
        lousa.configure(bg="#f8f9fa")

        tk.Label(lousa, text="📝 Texto do Release:",
                font=("Arial", 10, "bold"),
                bg="#f8f9fa").pack(pady=(10, 0))

        frame_texto = tk.Frame(lousa)
        frame_texto.pack(expand=True, fill='both', padx=15, pady=10)

        txt_area = tk.Text(frame_texto,
                        wrap='word',
                        font=("Arial", 11),
                        height=12,
                        padx=15,
                        pady=15,
                        relief="flat")

        scroll = tk.Scrollbar(frame_texto, command=txt_area.yview)
        txt_area.configure(yscrollcommand=scroll.set)

        txt_area.insert('1.0', texto_release)
        txt_area.pack(side='left', expand=True, fill='both')
        scroll.pack(side='right', fill='y')

        def copiar_para_clipboard():
            conteudo = txt_area.get('1.0', tk.END).strip()
            lousa.clipboard_clear()
            lousa.clipboard_append(conteudo)
            btn_copiar.config(text="✅ COPIADO!", bg="#27ae60")
            lousa.after(2000, lambda: btn_copiar.config(
                text="📋 COPIAR TEXTO", bg="#2ecc71"))

        btn_copiar = tk.Button(lousa,
                            text="📋 COPIAR TEXTO",
                            command=copiar_para_clipboard,
                            bg="#2ecc71",
                            fg="white",
                            font=("Arial", 11, "bold"),
                            height=2)
        btn_copiar.pack(fill='x', padx=15, pady=10)

        # ===============================
        # 🔥 SEÇÃO DE FOTOS NO RELEASE
        # ===============================

        if fotos:
            tk.Label(lousa,
                    text="📸 Fotos extraídas do PDF:",
                    font=("Arial", 9, "bold"),
                    bg="#f8f9fa").pack(pady=(5, 0))

            canvas = tk.Canvas(lousa, height=180,
                            bg="#f8f9fa",
                            highlightthickness=0)

            scroll_x = tk.Scrollbar(lousa,
                                    orient="horizontal",
                                    command=canvas.xview)

            frame_fotos = tk.Frame(canvas, bg="#f8f9fa")

            canvas.create_window((0, 0),
                                window=frame_fotos,
                                anchor="nw")

            canvas.configure(xscrollcommand=scroll_x.set)

            canvas.pack(fill="x", padx=15)
            scroll_x.pack(fill="x", padx=15, pady=(0, 10))

            image_refs = []

            for path in fotos:
                try:
                    img = Image.open(path)
                    img.thumbnail((150, 150))
                    photo = ImageTk.PhotoImage(img)
                    image_refs.append(photo)

                    lbl = tk.Label(frame_fotos,
                                image=photo,
                                bg="#f8f9fa")
                    lbl.pack(side='left', padx=8)

                except Exception as e:
                    print("Erro ao carregar imagem:", e)

            frame_fotos.update_idletasks()
            canvas.config(scrollregion=canvas.bbox("all"))

            # manter referência viva
            lousa.image_refs = image_refs

            btn_pasta = tk.Button(lousa,
                                text="📂 ABRIR PASTA DAS FOTOS",
                                command=lambda: abrir_pasta(self.download_path),
                                bg="#f39c12",
                                fg="white",
                                font=("Arial", 9, "bold"))
            btn_pasta.pack(pady=(5, 15))

    def mostrar_lousa_instagram(self, texto_inicial="", fotos_iniciais=None):
        self.fotos_selecionadas = [] # Lista para manter a ordem
        self.image_refs = []
        self.labels_numeros = {}
        self.frames_fotos = {} # Para gerenciar os widgets das fotos

        lousa = tk.Toplevel()
        lousa.title("Publicar no Instagram")
        lousa.geometry("850x850")
        lousa.configure(bg="#f8f9fa")

        tk.Label(lousa, text="📝 Legenda do Post (Tratada pela IA):", font=("Arial", 10, "bold"), bg="#f8f9fa").pack(pady=(10,0))

        frame_texto = tk.Frame(lousa)
        frame_texto.pack(expand=True, fill='both', padx=15, pady=10)

        txt_area = tk.Text(frame_texto, wrap='word', font=("Arial", 11), height=10, padx=15, pady=15, relief="flat")
        scroll = tk.Scrollbar(frame_texto, command=txt_area.yview)
        txt_area.configure(yscrollcommand=scroll.set)

        txt_area.insert('1.0', texto_inicial)
        txt_area.pack(side='left', expand=True, fill='both')
        scroll.pack(side='right', fill='y')

        # --- SEÇÃO DE FOTOS ---
        tk.Label(lousa, text="📸 Selecione na ordem desejada (Clique para remover):",
                 font=("Arial", 9, "bold"), bg="#f8f9fa").pack(pady=(5,0))

        canvas_fotos = tk.Canvas(lousa, height=180, bg="#f8f9fa", highlightthickness=0)
        scroll_x = tk.Scrollbar(lousa, orient="horizontal", command=canvas_fotos.xview)
        self.frame_fotos_container = tk.Frame(canvas_fotos, bg="#f8f9fa")

        canvas_fotos.create_window((0, 0), window=self.frame_fotos_container, anchor="nw")
        canvas_fotos.configure(xscrollcommand=scroll_x.set)

        canvas_fotos.pack(fill="x", padx=15)
        scroll_x.pack(fill="x", padx=15, pady=(0,5))

        def atualizar_numeros():
            for caminho in self.labels_numeros:
                self.labels_numeros[caminho].config(text="", bg="#f8f9fa")
            for i, caminho in enumerate(self.fotos_selecionadas, 1):
                if caminho in self.labels_numeros:
                    self.labels_numeros[caminho].config(text=str(i), fg="white", bg="#27ae60")

        def gerenciar_selecao(caminho, frame_widget):
            if caminho in self.fotos_selecionadas:
                self.fotos_selecionadas.remove(caminho)
                frame_widget.config(bg="#f8f9fa")
            else:
                if len(self.fotos_selecionadas) >= 10:
                    messagebox.showwarning("Limite", "O Instagram permite apenas 10 fotos!")
                    return
                self.fotos_selecionadas.append(caminho)
                frame_widget.config(bg="#d1e7dd")
            atualizar_numeros()

        def carregar_fotos_na_ui(fotos):
            for path in fotos:
                try:
                    img = Image.open(path)
                    img.thumbnail((110, 110))
                    photo = ImageTk.PhotoImage(img)
                    self.image_refs.append(photo)

                    f_borda = tk.Frame(self.frame_fotos_container, bg="#f8f9fa", padx=4, pady=4)
                    f_borda.pack(side='left', padx=5)

                    lbl_num = tk.Label(f_borda, text="", font=("Arial", 10, "bold"), bg="#f8f9fa")
                    lbl_num.pack(fill='x')
                    self.labels_numeros[path] = lbl_num

                    lbl_img = tk.Label(f_borda, image=photo, cursor="hand2")
                    lbl_img.pack()

                    callback = lambda e, p=path, w=f_borda: gerenciar_selecao(p, w)
                    lbl_img.bind("<Button-1>", callback)
                    lbl_num.bind("<Button-1>", callback)

                except Exception as e:
                    print(f"Erro ao carregar imagem: {e}")

            self.frame_fotos_container.update_idletasks()
            canvas_fotos.config(scrollregion=canvas_fotos.bbox("all"))

        if fotos_iniciais:
            carregar_fotos_na_ui(fotos_iniciais)

        def abrir_pasta_fotos():
            try:
                abrir_pasta(self.download_path)
            except Exception as e:
                messagebox.showerror("Erro", f"Não foi possível abrir a pasta:\n{e}")

        def publicar_insta():
            cfg = carregar_config()
            INSTA_ID = cfg.get("insta_id")
            TOKEN_EAAG = cfg.get("insta_token")
            # Validação rápida do token antes de iniciar o processo
            info_tk = verificar_token_instagram((TOKEN_EAAG or "").strip()) if TOKEN_EAAG else {"is_valid": False}
            if not INSTA_ID or not (TOKEN_EAAG or "").strip():
                messagebox.showwarning("Configuração", "Configure o Instagram ID e o Token em ⚙ Configurações.")
                return
            if info_tk.get("is_valid") is False:
                messagebox.showerror("Token inválido", "O token do Instagram parece inválido. Gere um novo token e salve em ⚙ Configurações.")
                return
            if not self.fotos_selecionadas:
                messagebox.showwarning("Aviso", "Selecione ao menos uma foto primeiro!")
                return

            def tarefa():
                try:
                    legend = txt_area.get('1.0', tk.END)
                    container_ids = []

                    for i, path in enumerate(self.fotos_selecionadas, 1):
                        self.escrever_log(f"Cloudinary: Subindo foto {i}/{len(self.fotos_selecionadas)}...")
                        res_cloud = cloudinary.uploader.upload(
                            path,
                            use_filename=True,
                            unique_filename=True,
                            resource_type="image",
                            transformation=[
                                {
                                    'width': 1080,
                                    'height': 1350,
                                    'crop': "fill",
                                    'gravity': "auto",
                                    'quality': "auto:best",
                                    'fetch_format': "auto"
                                }
                            ]
                        )
                        url_img = res_cloud['secure_url']

                        self.escrever_log(f"Aguardando propagação da URL...")
                        time.sleep(3)

                        self.escrever_log(f"Meta: Gerando container da foto {i}...")
                        payload = {
                            'image_url': url_img,
                            'is_carousel_item': 'true' if len(self.fotos_selecionadas) > 1 else 'false',
                            'access_token': TOKEN_EAAG
                        }

                        res = requests.post(f"https://graph.facebook.com/v19.0/{INSTA_ID}/media", data=payload).json()

                        if 'error' in res and res['error'].get('code') == 9004:
                            self.escrever_log("Erro de URI. Tentando novamente em 5s...")
                            time.sleep(5)
                            res = requests.post(f"https://graph.facebook.com/v19.0/{INSTA_ID}/media", data=payload).json()

                        if 'id' in res:
                            container_ids.append(res['id'])
                        else:
                            error_msg = res.get('error', {}).get('message', 'Erro desconhecido')
                            raise Exception(f"Erro na foto {i}: {error_msg}")

                    self.escrever_log("Criando postagem final...")
                    if len(container_ids) == 1:
                        payload_final = {
                            'image_url': url_img,
                            'caption': legend,
                            'access_token': TOKEN_EAAG
                        }
                    else:
                        payload_final = {
                            'media_type': 'CAROUSEL',
                            'children': ','.join(container_ids),
                            'caption': legend,
                            'access_token': TOKEN_EAAG
                        }

                    res_final = requests.post(f"https://graph.facebook.com/v19.0/{INSTA_ID}/media", data=payload_final).json()

                    if 'id' in res_final:
                        self.escrever_log("Aguardando processamento final...")
                        time.sleep(15)

                        pub = requests.post(f"https://graph.facebook.com/v19.0/{INSTA_ID}/media_publish",
                                          data={'creation_id': res_final['id'], 'access_token': TOKEN_EAAG}).json()

                        if 'id' in pub:
                            messagebox.showinfo("Sucesso", f"Publicado com sucesso! ({len(container_ids)} fotos)")
                            self.escrever_log("Instagram atualizado!")
                        else:
                            messagebox.showerror("Erro Publicação", str(pub))
                    else:
                        messagebox.showerror("Erro Container Final", str(res_final))

                except Exception as e:
                    messagebox.showerror("Erro no Processo", str(e))
                    self.escrever_log(f"Falha: {e}")

            threading.Thread(target=tarefa, daemon=True).start()

        btn_add = tk.Button(lousa, text="📂 ABRIR PASTA DAS FOTOS", command=abrir_pasta_fotos,
                             bg="#f39c12", fg="white", font=("Arial", 11, "bold"), pady=10)
        btn_add.pack(fill='x', padx=15, pady=5)

        btn_insta = tk.Button(lousa, text="🚀 PUBLICAR NO INSTAGRAM", command=publicar_insta,
                              bg="#833ab4", fg="white", font=("Arial", 11, "bold"), pady=10)
        btn_insta.pack(fill='x', padx=15, pady=5)

    def extrair_pdf_e_gerar(self, para_instagram=False, somente_processar=False, cancel_event=None, progress_cb=None):
        self.escrever_log("Aguardando download do PDF...")
        try:
            if progress_cb:
                progress_cb(10)
        except Exception:
            pass

        self._check_cancel(cancel_event)

        tempo_max = 30
        tempo_inicial = time.time()
        caminho_pdf = None

        while time.time() - tempo_inicial < tempo_max:
            self._check_cancel(cancel_event)
            arquivos = [f for f in os.listdir(self.download_path) if f.endswith('.pdf')]

            if arquivos:
                caminho_pdf = os.path.join(self.download_path, arquivos[-1])

                tamanho1 = os.path.getsize(caminho_pdf)
                time.sleep(1)
                tamanho2 = os.path.getsize(caminho_pdf)

                if tamanho1 == tamanho2 and tamanho1 > 0:
                    break

            time.sleep(1)

        if not caminho_pdf:
            self.escrever_log("PDF não encontrado.")
            return

        try:
            if progress_cb:
                progress_cb(20)
        except Exception:
            pass

        fotos_extraidas = []

        try:
            doc = fitz.open(caminho_pdf)

            for i, pagina in enumerate(doc):
                self._check_cancel(cancel_event)
                for img_index, img in enumerate(pagina.get_images(full=True)):
                    self._check_cancel(cancel_event)
                    xref = img[0]
                    base_image = doc.extract_image(xref)

                    if base_image["width"] < 250 or base_image["height"] < 250:
                        continue

                    image_bytes = base_image["image"]
                    ext = base_image["ext"]

                    nome_foto = f"registro_{i}_{img_index}.{ext}"
                    caminho_foto = os.path.join(self.download_path, nome_foto)

                    with open(caminho_foto, "wb") as f:
                        f.write(image_bytes)

                    fotos_extraidas.append(caminho_foto)

            doc.close()

            try:
                if progress_cb:
                    progress_cb(20)
            except Exception:
                pass

            tipo = "instagram" if para_instagram else "release"
            texto = self.gerar_texto_com_pdf(caminho_pdf, tipo, cancel_event=cancel_event)
            try:
                if progress_cb:
                    progress_cb(30)
            except Exception:
                pass

            if not texto:
                self.escrever_log("Falha ao gerar texto.")
                return

            try:
                if progress_cb:
                    progress_cb(20)
            except Exception:
                pass

            # Se solicitado, apenas devolve dados (útil para chamar a UI no thread principal)
            if somente_processar:
                return texto, fotos_extraidas

            self._check_cancel(cancel_event)

            if para_instagram:
                self.mostrar_lousa_instagram(texto, fotos_extraidas)
            else:
                self.mostrar_lousa_release(texto, fotos_extraidas)

            return texto, fotos_extraidas

        except CancelledError:
            raise

        except Exception as e:
            self.escrever_log(f"Erro ao processar PDF: {e}")
            return None

    def consultar_ocorrencias(self, user, pw, data_busca, municipio_busca, unidade_busca, cancel_event=None, progress_cb=None):
        self.escrever_log("Acessando Portal REDS...")
        self._garantir_navegador()
        try:
            if progress_cb:
                progress_cb(10)
        except Exception:
            pass
        self._check_cancel(cancel_event)

        if self.sessao_autenticada():
            self.escrever_log("Reutilizando a sessão autenticada do REDS.")
            self.fechar_avisos()
        else:
            self.driver.get(self.REDS_HOME_URL)
            self.esperar_pagina_carregar()
            self._check_cancel(cancel_event)

            if not self.sessao_autenticada() and not pw:
                raise RuntimeError("A sessão do REDS expirou. Informe a senha e tente novamente.")

            self.escrever_log(f"Realizando login para o usuário: {user}")
            resultado_login = self.fazer_login_seguro(user, pw, cancel_event=cancel_event)

            if resultado_login == "credencial_invalida":
                raise RuntimeError("Usuário ou senha recusados pelo portal REDS.")

            if resultado_login == "falha_tecnica":
                raise RuntimeError(
                    "Falha técnica no login. Verifique a conexão e tente novamente."
                )

            self.last_login_at = time.time()

        try:
            if progress_cb:
                progress_cb(10)
        except Exception:
            pass
        self._check_cancel(cancel_event)

        self.escrever_log("Navegando até 'Consulta Avançada'...")
        try:
            if progress_cb:
                progress_cb(10)
        except Exception:
            pass
        self.fechar_avisos()
        self.driver.get(self.REDS_ADVANCED_SEARCH_URL)
        self.esperar_pagina_carregar()

        if self._pagina_de_login():
            raise RuntimeError("A sessão do REDS expirou durante a navegação. Informe a senha novamente.")

        self.escrever_log(f"Inserindo data de busca: {data_busca}")
        try:
            if progress_cb:
                progress_cb(5)
        except Exception:
            pass
        campo_data = self.wait.until(EC.visibility_of_element_located((By.NAME, "dataInicialCriacao")))
        self._preencher_campo(campo_data, data_busca)
        campo_data.send_keys(Keys.TAB)

        self.escrever_log("Filtrando Órgão (Bombeiros)...")
        try:
            if progress_cb:
                progress_cb(5)
        except Exception:
            pass
        orgaos = self.driver.find_elements(By.NAME, "id_orgao_selecionado")
        if not any(elemento.is_displayed() for elemento in orgaos):
            self.wait.until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//legend[contains(@class,'toggleDiv') and "
                        "contains(normalize-space(.),'Unidade Responsável pelo Registro')]",
                    )
                )
            ).click()
        orgao = self.wait.until(EC.visibility_of_element_located((By.NAME, "id_orgao_selecionado")))
        self.driver.execute_script(
            "arguments[0].value='2';"
            "if(window.jQuery){window.jQuery(arguments[0]).trigger('change');}"
            "else{arguments[0].dispatchEvent(new Event('change',{bubbles:true}));}",
            orgao,
        )
        self.esperar_loading()

        # --- BLOCO DO MUNICÍPIO (DINÂMICO COM VALIDAÇÃO OKMINI) ---
        self.escrever_log(f"Selecionando Município: {municipio_busca}...")
        try:
            if progress_cb:
                progress_cb(10)
        except Exception:
            pass
        mun = self.wait.until(EC.visibility_of_element_located((By.ID, "treeSearchMunicipioResp")))

        self._preencher_campo(mun, municipio_busca)

        # Tenta selecionar da lista caso apareçam múltiplas opções
        try:
            # Espera curta para ver se a lista de busca (jstree-search) aparece
            xpath_busca = f"//div[@id='treeMunicipioResp']//a[contains(@class, 'jstree-search') and contains(@title, '{municipio_busca.upper()}')]"
            item_filtrado = WebDriverWait(self.driver, 3).until(EC.element_to_be_clickable((By.XPATH, xpath_busca)))
            self.driver.execute_script("arguments[0].click();", item_filtrado)
            self.escrever_log("Município selecionado da lista de múltiplas opções.")
        except:
            # Se não encontrou lista, tenta apenas dar um ENTER para validar o nome único
            self.escrever_log("Lista de múltiplas opções não detectada. Validando entrada única...")
            mun.send_keys(Keys.ENTER)

        # AGUARDA A VALIDAÇÃO FINAL (O check verde 'okMini')
        try:
            self.escrever_log("Aguardando validação do município pelo sistema...")
            validacao_xpath = "//span[@id='statusMunicipioResp' and contains(@class, 'okMini')]"
            self.wait.until(EC.presence_of_element_located((By.XPATH, validacao_xpath)))
            self.escrever_log("Município validado com sucesso!")
            try:
                if progress_cb:
                    progress_cb(5)
            except Exception:
                pass
        except Exception as e:
            self.escrever_log(f"O sistema não validou o município a tempo: {e}")


        self.esperar_loading()
        # --- BLOCO DA UNIDADE (DINÂMICO POR BUSCA) ---
        self.escrever_log(f"Selecionando Unidade: {unidade_busca}...")
        try:
            if progress_cb:
                progress_cb(10)
        except Exception:
            pass

        campo_unid = self.wait.until(EC.element_to_be_clickable((By.ID, "treeSearchUnidResp")))
        self.driver.execute_script("arguments[0].scrollIntoView(true);", campo_unid)

        self._preencher_campo(campo_unid, unidade_busca)

        # ✅ Aguarda a árvore de unidades carregar antes de tentar clicar (evita erro "rápido demais")
        try:
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.XPATH, "//div[@id='treeUnidResp']//ul//li"))
            )
        except Exception:
            # Se não carregou, ainda tentamos seguir (o ENTER/fallback pode funcionar)
            self.escrever_log("Árvore de unidades ainda carregando; tentando seleção mesmo assim...")

        # Tenta selecionar item filtrado na árvore
        try:
            xpath_unid = f"//div[@id='treeUnidResp']//a[contains(@class, 'jstree-search') and contains(translate(@title,'abcdefghijklmnopqrstuvwxyzáàâãéêíóôõúç','ABCDEFGHIJKLMNOPQRSTUVWXYZÁÀÂÃÉÊÍÓÔÕÚÇ'), '{unidade_busca.upper()}')]"
            item_unid = WebDriverWait(self.driver, 4).until(EC.element_to_be_clickable((By.XPATH, xpath_unid)))
            self.driver.execute_script("arguments[0].click();", item_unid)
            self.escrever_log("Unidade selecionada da lista filtrada.")
        except Exception:
            # Fallback: ENTER e depois tenta clicar no primeiro item (caso o sistema não liste)
            try:
                campo_unid.send_keys(Keys.ENTER)
            except Exception:
                pass
            try:
                xpath_item = "//div[@id='treeUnidResp']//ul/li[1]/a"
                primeiro_item = WebDriverWait(self.driver, 3).until(EC.element_to_be_clickable((By.XPATH, xpath_item)))
                self.driver.execute_script("arguments[0].click();", primeiro_item)
                self.escrever_log("Unidade selecionada via fallback (primeiro item).")
            except Exception as e:
                self.escrever_log(f"Aguarde...")

        # Aguarda validação (quando existir)
        try:
            self.escrever_log("Aguardando validação da unidade pelo sistema...")
            valid_unid_xpath = "//span[@id='statusUnidResp' and contains(@class, 'okMini')]"
            WebDriverWait(self.driver, 6).until(EC.presence_of_element_located((By.XPATH, valid_unid_xpath)))
            self.escrever_log("Unidade validada com sucesso!")
            try:
                if progress_cb:
                    progress_cb(5)
            except Exception:
                pass
        except Exception:
            # Nem sempre existe o marcador; segue fluxo
            pass
        # -------------------------------------------------

        self.esperar_loading()
        self.escrever_log("Executando consulta...")
        try:
            if progress_cb:
                progress_cb(10)
        except Exception:
            pass
        cb = self.wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//div[@id='div_unid_resp']//input[@type='checkbox']")
            )
        )
        if not cb.is_selected():
            self.driver.execute_script("arguments[0].click();", cb)
        consultar = self.wait.until(EC.element_to_be_clickable((By.NAME, "consultar")))
        self.driver.execute_script("arguments[0].click();", consultar)
        self.esperar_loading()

        self.escrever_log("Verificando resultado da consulta...")
        try:
            if progress_cb:
                progress_cb(5)
        except Exception:
            pass

        def resultado_pronto(driver):
            for elemento in driver.find_elements(By.CSS_SELECTOR, "div.boldLabel"):
                try:
                    texto = elemento.text.strip()
                    if elemento.is_displayed() and "Nenhum registro foi encontrado" in texto:
                        return "vazio", texto
                except Exception:
                    continue

            for tabela in driver.find_elements(By.ID, "resumeCollectionTable"):
                try:
                    if tabela.is_displayed():
                        return "tabela", tabela
                except Exception:
                    continue

            for seletor in (".alert-danger", ".errorMessage", ".ui-messages-error-summary"):
                for elemento in driver.find_elements(By.CSS_SELECTOR, seletor):
                    try:
                        texto = elemento.text.strip()
                        if elemento.is_displayed() and texto:
                            return "erro", texto
                    except Exception:
                        continue
            return False

        try:
            estado, detalhe = WebDriverWait(self.driver, 40).until(resultado_pronto)
        except TimeoutException as e:
            raise RuntimeError(
                "O REDS não apresentou a tabela nem uma mensagem de resultado a tempo."
            ) from e

        if estado == "vazio":
            self.escrever_log(
                "Nenhum registro encontrado para os critérios informados. Verifique a data selecionada."
            )
            return {}
        if estado == "erro":
            raise RuntimeError(f"O REDS recusou a consulta: {detalhe}")

        try:
            if progress_cb:
                progress_cb(5)
        except Exception:
            pass

        self._check_cancel(cancel_event)

        linhas = self.driver.find_elements(By.XPATH, "//table[@id='resumeCollectionTable']/tbody/tr")
        try:
            if progress_cb:
                progress_cb(5)
        except Exception:
            pass

        mapa_dados = {}
        contador_visual = 1
        for linha in linhas:
            self._check_cancel(cancel_event)
            cols = linha.find_elements(By.TAG_NAME, "td")
            if len(cols) < 8: continue
            reds_val = cols[1].text.strip()
            if not reds_val: continue
            dh_bruto = cols[3].text.strip().split(" ")
            nat_bruta = cols[4].text.strip()
            nat_limpa = re.sub(r'\(.*?\)', '', nat_bruta).strip()
            end_completo = cols[5].text.strip()
            situacao = cols[7].text.strip()
            pendente = ("Pendente de Elaboração" in situacao) or ("Aberto" in situacao)

            mapa_dados[contador_visual] = {
                'reds': reds_val,
                'btn': cols[0].find_element(By.TAG_NAME, "input"),
                'reds': reds_val,
                'natureza': nat_limpa,
                'endereco': end_completo,
                'data': dh_bruto[0],
                'hora': dh_bruto[1] if len(dh_bruto) > 1 else "00:00",
                'pendente': pendente
            }
            status_msg = "[BLOQUEADO]" if pendente else "[OK]"
            self.escrever_log(
                f"{contador_visual:<3} | {reds_val:<20} | "
                f"{mapa_dados[contador_visual]['hora']:<6} | {status_msg} {nat_limpa[:40]}"
            )
            contador_visual += 1
        try:
            if progress_cb:
                progress_cb(5)
        except Exception:
            pass
        return mapa_dados

# --- INTERFACE UNIFICADA ---

def iniciar_interface():
    root = tk.Tk()
    root.title("Assistente de imprensa — CBMMG (SISP/REDS)")

    # Ajuste para telas menores: garante que a janela caiba na tela e mantenha a barra de status/progresso visível
    largura_pref, altura_pref = 980, 780
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    largura = min(largura_pref, max(780, sw - 80))
    altura = min(altura_pref, max(600, sh - 90))

    posx = (sw - largura) // 2
    posy = (sh - altura) // 2
    root.geometry(f"{largura}x{altura}+{posx}+{posy}")
    root.minsize(760, 600)

    try:
        root.iconbitmap(recurso_caminho("logo.ico"))
    except Exception:
        pass

    # ===============================
    # 🎨 Tema moderno (claro/escuro)
    # ===============================
    style = ttk.Style(root)
    # Estilo padronizado para botões principais
    try:
        style.configure("Primary.TButton", padding=(12, 6))
    except Exception:
        pass

    try:
        style.theme_use("clam")  # mais "personalizável" que o padrão
    except Exception:
        pass

    # Barra de progresso (cor + visual mais agradável)
    # Obs.: o tema "clam" permite personalização das cores no ttk.
    try:
        style.configure(
            "Green.Horizontal.TProgressbar",
            troughcolor="#E6E9EF",
            background="#2ECC71",
            bordercolor="#E6E9EF",
            lightcolor="#2ECC71",
            darkcolor="#27AE60",
            thickness=10,
        )
    except Exception:
        pass
    # Estilos para manter botões na mesma altura dos campos (Entry/Combobox)
    try:
        style.configure("Small.TButton", padding=(10, 0))
        style.configure("Mini.TButton", padding=(6, 0))
    except Exception:
        pass

    # Botão pequeno (olho) — menos padding para não deformar o layout
    try:
        style.configure("Eye.TButton", padding=(2, 0))
    except Exception:
        pass



    paletas = {
        "light": {
            "bg": "#F5F6FA",
            "card": "#FFFFFF",
            "fg": "#111827",
            "muted": "#6B7280",
            "border": "#E5E7EB",
            "header": "#EEF2FF",
            "entry": "#FFFFFF",
            "accent": "#2563EB",
            "primary": "#2563EB",
            "primary_hover": "#1D4ED8",
            "primary_pressed": "#1E40AF",
            "success": "#16A34A",
            "success_hover": "#15803D",
            "success_pressed": "#166534",
            "insta": "#8546A0",
            "insta_hover": "#6D2F86",
            "insta_pressed": "#5B2573",
            "danger": "#DC2626",
            "log_bg": "#111827",
            "log_fg": "#E5E7EB",
            "warn_row": "#FEE2E2",
        },
        "dark": {
            "bg": "#0B1220",
            "card": "#0F172A",
            "fg": "#E5E7EB",
            "muted": "#94A3B8",
            "border": "#1F2937",
            "header": "#111827",
            "entry": "#0B1220",
            "accent": "#3B82F6",
            "primary": "#3B82F6",
            "primary_hover": "#2563EB",
            "primary_pressed": "#1D4ED8",
            "success": "#22C55E",
            "success_hover": "#16A34A",
            "success_pressed": "#15803D",
            "insta": "#A855F7",
            "insta_hover": "#9333EA",
            "insta_pressed": "#7E22CE",
            "danger": "#F87171",
            "log_bg": "#020617",
            "log_fg": "#E2E8F0",
            "warn_row": "#3F1D1D",
        },
    }

    cfg = carregar_config()
    tema_atual = cfg.get("ui_theme", "light")
    if tema_atual not in paletas:
        tema_atual = "light"

    tema_var = tk.BooleanVar(value=(tema_atual == "dark"))
    status_var = tk.StringVar(value="Pronto.")
    insta_token_status_var = tk.StringVar(value="Token do Instagram: verificando...")
    filtro_var = tk.StringVar(value="")
    lembrar_user_var = tk.BooleanVar(value=bool(cfg.get("ui_remember_user", False)))
    busy_state = {"ativo": False}

    # Status bar (resumo executivo)
    _ctx_ui = {"municipio": "", "unidade": "", "data": ""}

    def _set_status(msg: str = None, exibidos: int = None, total: int = None):
        try:
            mun = _ctx_ui.get("municipio", "")
            uni = _ctx_ui.get("unidade", "")
            dat = _ctx_ui.get("data", "")
            left = " | ".join([p for p in [
                f"Município: {mun}" if mun else "",
                f"Unidade: {uni}" if uni else "",
                f"Data: {dat}" if dat else ""
            ] if p])
            right = ""
            if exibidos is not None and total is not None:
                right = f"Registros: {exibidos}/{total}"
            base = (msg or "").strip()
            parts = [p for p in [left, right, base] if p]
            status_var.set("   •   ".join(parts) if parts else "Pronto.")
        except Exception:
            pass
    def ui(callable_):
        """Executa algo no thread da UI (Tk)."""
        root.after(0, callable_)

    # Tooltip simples (sem libs externas)
    class Tooltip:
        def __init__(self, widget, text: str, delay_ms: int = 450, autohide_ms: int = 2200):
            self.widget = widget
            self.text = text
            self.tip = None
            self._after_id = None
            self._autohide_id = None
            self.delay_ms = delay_ms
            self.autohide_ms = autohide_ms

            widget.bind("<Enter>", self._schedule)
            widget.bind("<Leave>", self._hide)
            widget.bind("<ButtonPress>", self._hide)

        def _schedule(self, _ev=None):
            self._cancel_scheduled()
            if not self.text:
                return
            self._after_id = self.widget.after(self.delay_ms, self._show)

        def _cancel_scheduled(self):
            try:
                if self._after_id:
                    self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
            try:
                if self._autohide_id:
                    self.widget.after_cancel(self._autohide_id)
            except Exception:
                pass
            self._autohide_id = None

        def _show(self):
            if self.tip or not self.text:
                return
            try:
                x = self.widget.winfo_rootx() + 12
                y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
                self.tip = tk.Toplevel(self.widget)
                self.tip.wm_overrideredirect(True)
                try:
                    self.tip.attributes("-topmost", True)
                except Exception:
                    pass
                self.tip.wm_geometry(f"+{x}+{y}")
                lbl = tk.Label(
                    self.tip, text=self.text, justify="left",
                    bg="#111827", fg="#F9FAFB",
                    padx=8, pady=4, font=("Segoe UI", 9)
                )
                lbl.pack()
                self.tip.bind("<Leave>", self._hide)
                self._autohide_id = self.widget.after(self.autohide_ms, self._hide)
            except Exception:
                self.tip = None

        def _hide(self, _ev=None):
            self._cancel_scheduled()
            try:
                if self.tip:
                    self.tip.destroy()
            except Exception:
                pass
            self.tip = None
    def _formatar_token_status():
        cfg_local = carregar_config()
        token = (cfg_local.get("insta_token") or "").strip()

        # 1) Verifica token (online, quando possível)
        info = verificar_token_instagram(token) if token else {"is_valid": False, "expires_at": None, "source": "error", "error": "Token não configurado."}

        # 2) Decide expiração (prioriza API; senão usa config/estimativa)
        exp_dt = info.get("expires_at")
        exp_src = info.get("source")
        if exp_dt is None:
            exp_dt2, src2 = estimar_expiracao_token(cfg_local)
            if exp_dt2 is not None:
                exp_dt, exp_src = exp_dt2, src2

        if not token:
            return "Token do Instagram: não configurado."
        if info.get("is_valid") is False:
            return "Token do Instagram: inválido (verifique em ⚙ Configurações)."
        if info.get("is_valid") is None:
            return "Token do Instagram: não foi possível verificar agora."

        if exp_dt:
            agora = datetime.now()
            dias = int((exp_dt - agora).total_seconds() // 86400)
            if dias < 0:
                return "Token do Instagram: expirado (gere um novo token)."
            # texto leve, semelhante ao subtítulo
            if dias == 0:
                return "Token do Instagram: vence hoje."
            if dias == 1:
                return "Token do Instagram: vence em 1 dia."
            return f"Token do Instagram: vence em {dias} dias."
        else:
            return "Token do Instagram: válido."

    def _atualizar_token_label():
        try:
            insta_token_status_var.set(_formatar_token_status())
        except Exception:
            pass
        # Atualiza periodicamente (a cada 10 min)
        try:
            root.after(10 * 60 * 1000, _atualizar_token_label)
        except Exception:
            pass

    # Atualiza já ao abrir
    root.after(250, _atualizar_token_label)



    def aplicar_tema(modo: str):
        cores = paletas.get(modo, paletas["light"])
        root.configure(bg=cores["bg"])

        # Base
        style.configure(".", font=("Segoe UI", 10))
        style.configure("TFrame", background=cores["bg"])
        style.configure("Card.TFrame", background=cores["card"])
        style.configure("TLabel", background=cores["bg"], foreground=cores["fg"])
        style.configure("Muted.TLabel", background=cores["bg"], foreground=cores["muted"])
        style.configure("Title.TLabel", background=cores["bg"], foreground=cores["fg"], font=("Segoe UI", 13, "bold"))

        # Labelframe
        style.configure("TLabelframe", background=cores["bg"], foreground=cores["fg"])
        style.configure("TLabelframe.Label", background=cores["bg"], foreground=cores["fg"], font=("Segoe UI", 10, "bold"))

        # Inputs
        style.configure("TEntry", fieldbackground=cores["entry"], foreground=cores["fg"])
        style.configure("TCombobox", fieldbackground=cores["entry"], foreground=cores["fg"])

        # Botões (estilos)
        style.configure("TButton", padding=(12, 6))

        # Força mesma altura para botões do header
        style.configure("Header.TButton", padding=(10, 3))
        style.configure("Header.TCheckbutton", padding=(10, 4))
        style.configure("Primary.TButton", background=cores["primary"], foreground="white", borderwidth=0, padding=(12, 6))
        style.map("Primary.TButton",
                  background=[("active", cores["primary_hover"]), ("pressed", cores["primary_pressed"])],
                  foreground=[("disabled", "#9CA3AF")])

        # Hover sutil para botões padrão
        try:
            style.map("TButton", background=[("active", cores["header"])])
        except Exception:
            pass

        # Treeview (tabela) — visual mais "premium"
        try:
            style.configure(
                "Treeview",
                background=cores["card"],
                fieldbackground=cores["card"],
                foreground=cores["fg"],
                rowheight=26
            )
            style.configure(
                "Treeview.Heading",
                background=cores["header"],
                foreground=cores["fg"],
                relief="flat",
                font=("Segoe UI", 10, "bold")
            )
            style.map(
                "Treeview",
                background=[("selected", cores["accent"])],
                foreground=[("selected", "white")]
            )
        except Exception:
            pass

        # Zebra (linhas alternadas)
        try:
            tree.tag_configure("even", background=cores["card"], foreground=cores["fg"])
            tree.tag_configure("odd", background=cores["header"], foreground=cores["fg"])
        except Exception:
            pass


        style.configure("Success.TButton", background=cores["success"], foreground="white", borderwidth=0, padding=(12, 6))
        style.map("Success.TButton",
                  background=[("active", cores["success_hover"]), ("pressed", cores["success_pressed"])],
                  foreground=[("disabled", "#9CA3AF")])

        style.configure("Insta.TButton", background=cores["insta"], foreground="white", borderwidth=0, padding=(12, 6))
        style.map("Insta.TButton",
                  background=[("active", cores["insta_hover"]), ("pressed", cores["insta_pressed"])],
                  foreground=[("disabled", "#9CA3AF")])

        style.configure("Danger.TButton", background=cores["danger"], foreground="white", borderwidth=0)
        style.map("Danger.TButton",
                  background=[("active", cores["danger"]), ("pressed", cores["danger"])],
                  foreground=[("disabled", "#9CA3AF")])

        # Variante mais compacta (ex.: botão Cancelar na status bar)
        style.configure("DangerSmall.TButton", background=cores["danger"], foreground="white",
                        borderwidth=0, padding=(10, 3))
        style.map("DangerSmall.TButton",
                  background=[("active", cores["danger"]), ("pressed", cores["danger"])],
                  foreground=[("disabled", "#9CA3AF")])

        # Treeview
        style.configure("Treeview",
                        background=cores["card"],
                        fieldbackground=cores["card"],
                        foreground=cores["fg"],
                        rowheight=28,
                        bordercolor=cores["border"],
                        borderwidth=0)
        style.configure("Treeview.Heading",
                        background=cores["header"],
                        foreground=cores["fg"],
                        relief="flat",
                        font=("Segoe UI", 10, "bold"))
        style.map("Treeview",
                  background=[("selected", cores["accent"])],
                  foreground=[("selected", "white")])

        # Scrollbars
        style.configure("TScrollbar", troughcolor=cores["bg"], background=cores["border"], bordercolor=cores["bg"])

        # Log (widget tk.Text, então é na mão)
        try:
            log_box.configure(bg=cores["log_bg"], fg=cores["log_fg"], insertbackground=cores["log_fg"])
        except Exception:
            pass

        # Tag do Treeview (pendente/bloqueado) acompanha o tema
        try:
            tree.tag_configure("bloqueado", background=cores["warn_row"], foreground=cores["fg"])
        except Exception:
            pass

        # Ajuste de colunas para não amassar ID/Hora/Natureza em janela menor
        def _ajustar_colunas_tree(_ev=None):
            try:
                w = tree.winfo_width()
                if w <= 50:
                    return
                w_id = 50
                w_hora = 70
                w_nat = max(260, int(w * 0.34))
                w_end = max(300, w - (w_id + w_hora + w_nat + 30))
                tree.column("ID", width=w_id, minwidth=45, stretch=False, anchor="center")
                tree.column("Hora", width=w_hora, minwidth=60, stretch=False, anchor="center")
                tree.column("Natureza", width=w_nat, minwidth=200, stretch=False, anchor="w")
                tree.column("Endereço", width=w_end, minwidth=240, stretch=True, anchor="w")
            except Exception:
                pass

        tree.bind("<Configure>", _ajustar_colunas_tree)
        root.after(150, _ajustar_colunas_tree)
        root.after(100, _ajustar_colunas_tree)


        pass

    def alternar_tema():
        nonlocal tema_atual
        novo = "dark" if tema_var.get() else "light"
        tema_atual = novo
        aplicar_tema(novo)
        salvar_config({"ui_theme": novo})

    # ===============================
    # 🔝 Cabeçalho
    # ===============================
    header = ttk.Frame(root, padding=(14, 12, 14, 6))
    header.pack(fill="x")

    left_header = ttk.Frame(header)
    left_header.pack(side="left", fill="x", expand=True)

    ttk.Label(left_header, text="Assistente de Imprensa", style="Title.TLabel").pack(anchor="w")
    ttk.Label(left_header, text="Consulta no REDS + geração de release e postagem no Instagram", style="Muted.TLabel").pack(anchor="w", pady=(2, 0))
    ttk.Label(left_header, textvariable=insta_token_status_var, style="Muted.TLabel").pack(anchor="w", pady=(2, 0))

    right_header = ttk.Frame(header)
    right_header.pack(side="right")

    btn_tema = ttk.Button(
        right_header,
        text=("☀ Modo claro" if tema_var.get() else "🌙 Modo escuro"),
        style="Header.TButton",
        width=16,
    )

    def _toggle_tema():
        tema_var.set(not tema_var.get())
        alternar_tema()
        try:
            btn_tema.configure(text=("☀ Modo claro" if tema_var.get() else "🌙 Modo escuro"))
        except Exception:
            pass

    btn_tema.configure(command=_toggle_tema)
    btn_tema.pack(side="right", padx=(10, 0))
    # ===============================
    # 📦 Conteúdo
    # ===============================
    container = ttk.Frame(root, padding=(14, 0, 14, 14))
    container.pack(fill="both", expand=True)

    frm_filtros = ttk.Labelframe(container, text="Acesso e filtros", padding=12)
    frm_filtros.pack(fill="x")

    # Carregar municípios
    dados_municipios = carregar_municipios()
    lista_municipios = dados_municipios.get("municipios_salvos", [])
    ultimo_usado = dados_municipios.get("ultimo_usado", "")

    # Carregar unidades (por município)
    dados_unidades = carregar_unidades()
    unidades_por_municipio = dados_unidades.get("unidades_por_municipio", {})
    ultimo_unid_por_mun = dados_unidades.get("ultimo_por_municipio", {})

    # Usuário / senha (opcional lembrar usuário)
    # Usuário / senha (opcional lembrar usuário) — layout compacto
    ttk.Label(frm_filtros, text="Usuário SISP:").grid(row=0, column=0, sticky="w")

    frame_user = ttk.Frame(frm_filtros)
    frame_user.grid(row=1, column=0, sticky="we", padx=(0, 10), pady=(2, 8))
    frame_user.grid_columnconfigure(0, weight=1)

    ent_user = ttk.Entry(frame_user, width=18)
    ent_user.grid(row=0, column=0, sticky="we")

    if lembrar_user_var.get() and cfg.get("sisp_user"):
        ent_user.insert(0, cfg.get("sisp_user", ""))

    # ✅ Lembrar usuário (nunca salva senha)
    def on_toggle_lembrar_user():
        salvar_config({"ui_remember_user": bool(lembrar_user_var.get())})
        if not lembrar_user_var.get():
            salvar_config({"sisp_user": ""})

    chk_user = ttk.Checkbutton(
        frame_user,
        text="Lembrar",
        variable=lembrar_user_var,
        command=on_toggle_lembrar_user
    )
    chk_user.grid(row=0, column=1, sticky="w", padx=(8, 0))


    ttk.Label(frm_filtros, text="Senha:").grid(row=0, column=1, sticky="w")

    frame_pw = ttk.Frame(frm_filtros)
    frame_pw.grid(row=1, column=1, sticky="we", padx=(0, 10), pady=(2, 8))
    frame_pw.grid_columnconfigure(0, weight=1)

    ent_pw = ttk.Entry(frame_pw, width=18, show="*")
    ent_pw.grid(row=0, column=0, sticky="we")

    # Data (DateEntry se existir; senão Entry)

    ttk.Label(frm_filtros, text="Data:").grid(row=0, column=2, sticky="w")

    frame_data = ttk.Frame(frm_filtros)
    frame_data.grid(row=1, column=2, sticky="we", padx=(0, 10), pady=(2, 8))

    hoje = datetime.now()
    data_padrao = hoje.strftime("%d/%m/%Y")

    if DateEntry is not None:
        cal = DateEntry(frame_data, width=12, background='darkblue', foreground='white',
                        borderwidth=2, date_pattern='dd/mm/yyyy')
        cal.set_date(hoje)
        cal.pack(side="left")
        ent_data_fallback = None
    else:
        ent_data_fallback = ttk.Entry(frame_data, width=12)
        ent_data_fallback.insert(0, data_padrao)
        ent_data_fallback.pack(side="left")
        cal = None

    def _set_data(delta_dias: int):
        dt = datetime.now() + timedelta(days=delta_dias)
        if cal is not None:
            try:
                cal.set_date(dt)
            except Exception:
                pass
        elif ent_data_fallback is not None:
            ent_data_fallback.delete(0, tk.END)
            ent_data_fallback.insert(0, dt.strftime("%d/%m/%Y"))

    ttk.Button(frame_data, text="Hoje", style="Small.TButton", command=lambda: _set_data(0)).pack(side="left", padx=(8, 0))
    ttk.Button(frame_data, text="Ontem", style="Small.TButton", command=lambda: _set_data(-1)).pack(side="left", padx=(6, 0))

    def obter_data():
        if cal is not None:
            return cal.get()
        return (ent_data_fallback.get().strip() if ent_data_fallback else data_padrao)


    # Município + Unidade + lembrar usuário (antes da busca)
    ttk.Label(frm_filtros, text="Município:").grid(row=0, column=3, sticky="w")
    ttk.Label(frm_filtros, text="Unidade:").grid(row=0, column=4, sticky="w")

    # Município (com histórico)
    frame_mun = ttk.Frame(frm_filtros)
    frame_mun.grid(row=1, column=3, sticky="we", pady=(2, 8))
    frame_mun.grid_columnconfigure(0, weight=1)

    ent_municipio = ttk.Combobox(frame_mun, width=24, values=lista_municipios)
    ent_municipio.grid(row=0, column=0, sticky="we")
    ent_municipio.set(ultimo_usado)

    def filtrar_municipios(_event=None):
        texto = ent_municipio.get().upper().strip()
        dados = carregar_municipios()
        lista = dados.get("municipios_salvos", [])
        if not texto:
            ent_municipio["values"] = lista
            return
        filtrados = [m for m in lista if texto in m]
        ent_municipio["values"] = filtrados if filtrados else lista

    ent_municipio.bind("<KeyRelease>", filtrar_municipios)

    # Unidade (por município) + checkbox lembrar usuário
    frame_unid = ttk.Frame(frm_filtros)
    frame_unid.grid(row=1, column=4, sticky="we", padx=(10, 0), pady=(2, 8))
    frame_unid.grid_columnconfigure(0, weight=1)

    ent_unidade = ttk.Combobox(frame_unid, width=26, values=[])
    ent_unidade.grid(row=0, column=0, sticky="we")


    def _normalizar_lista_unidades(mun: str):
        mun = (mun or "").strip().upper()
        lista = unidades_por_municipio.get(mun, [])
        # remove vazios e normaliza
        lista = [u.strip() for u in (lista or []) if str(u).strip()]
        # mantém ordem (sem set) para respeitar cadastro do usuário
        return lista

    def atualizar_unidades_para_municipio(mun: str):
        mun = (mun or "").strip().upper()
        lista = _normalizar_lista_unidades(mun)
        ent_unidade["values"] = lista

        # Seleciona última usada (se existir), senão primeira, senão vazio
        ultima = (ultimo_unid_por_mun.get(mun, "") or "").strip()
        if ultima and ultima in lista:
            ent_unidade.set(ultima)
        elif lista:
            ent_unidade.set(lista[0])
        else:
            ent_unidade.set("")

    def _on_municipio_change(_event=None):
        atualizar_unidades_para_municipio(ent_municipio.get())

    ent_municipio.bind("<<ComboboxSelected>>", _on_municipio_change)

    # Aplica unidades do município inicial
    atualizar_unidades_para_municipio(ultimo_usado)

    # Ajuste de grid
    frm_filtros.grid_columnconfigure(0, weight=1)
    frm_filtros.grid_columnconfigure(1, weight=1)
    frm_filtros.grid_columnconfigure(2, weight=1)
    frm_filtros.grid_columnconfigure(3, weight=2)
    frm_filtros.grid_columnconfigure(4, weight=2)


    # ===============================
    # 📐 Layout responsivo (evita texto "comido" em janelas menores)
    # ===============================
    _layout_filtros = {"compacto": None}
    _resize_job = {"id": None}


    def _aplicar_layout_filtros(compacto: bool):
        # O layout responsivo anterior reposicionava o checkbox "Lembrar usuário".
        # Como ele agora fica fixo abaixo do campo Usuário, não há nada a recalcular aqui.
        if _layout_filtros["compacto"] == compacto:
            return
        _layout_filtros["compacto"] = compacto
        return

    def _on_root_resize(_event=None):
        # Debounce para não recalcular layout a cada pixel
        try:
            if _resize_job["id"] is not None:
                root.after_cancel(_resize_job["id"])
        except Exception:
            pass

        def _recalc():
            # Breakpoint ajustável: aumente/diminua conforme seu gosto
            largura = root.winfo_width()
            _aplicar_layout_filtros(compacto=(largura < 1120))

        _resize_job["id"] = root.after(120, _recalc)

    root.bind("<Configure>", _on_root_resize)
    # Aplica uma vez no início
    root.after(0, _on_root_resize)

    # ===============================
    # 🧭 Barra de ações
    # ===============================
    frm_acoes = ttk.Frame(container, padding=(0, 10, 0, 10))
    frm_acoes.pack(fill="x")

    # (Carrega ícones, se existirem)
    try:
        img_sids = Image.open(recurso_caminho("SIDSFlatPequeno.png")).resize((80, 25), Image.LANCZOS)
        photo_sids = ImageTk.PhotoImage(img_sids)
    except Exception:
        photo_sids = None

    try:
        img_robot = Image.open(recurso_caminho("robot.png")).resize((25, 25), Image.LANCZOS)
        photo_robot = ImageTk.PhotoImage(img_robot)
    except Exception:
        photo_robot = None

    try:
        img_insta = Image.open(recurso_caminho("instagram.png")).resize((25, 25), Image.LANCZOS) if os.path.exists(recurso_caminho("instagram.png")) else None
        photo_insta = ImageTk.PhotoImage(img_insta) if img_insta else None
    except Exception:
        photo_insta = None

    for i in range(3):
        frm_acoes.grid_columnconfigure(i, weight=1, uniform='acoes')

    btn_busca = ttk.Button(frm_acoes, text="  Iniciar busca no REDS", image=photo_sids, compound="left", style="Primary.TButton")
    btn_busca.grid(row=0, column=0, sticky="ew")

    Tooltip(btn_busca, 'Consultar no REDS e carregar ocorrências.')
    btn_gerar = ttk.Button(frm_acoes, text="  Gerar release (selecionado)", image=photo_robot, compound="left", style="Success.TButton")
    btn_gerar.grid(row=0, column=1, sticky="ew", padx=(10, 0))
    btn_gerar.state(['disabled'])

    Tooltip(btn_gerar, 'Gera o release usando a ocorrência selecionada.')
    btn_insta_main = ttk.Button(frm_acoes, text="  Instagram (selecionado)", image=photo_insta, compound="left", style="Insta.TButton")
    btn_insta_main.grid(row=0, column=2, sticky="ew", padx=(10, 0))
    btn_insta_main.state(['disabled'])

    Tooltip(btn_insta_main, 'Gera texto para Instagram usando a ocorrência selecionada.')
    # manter referências vivas
    btn_busca.image = photo_sids
    btn_gerar.image = photo_robot
    btn_insta_main.image = photo_insta

    # ===============================
    # 📑 Área principal (tabela + log)
    # ===============================
    paned = ttk.PanedWindow(container, orient="vertical")
    paned.pack(fill="both", expand=True)

    # Painel da tabela
    frame_table_card = ttk.Labelframe(paned, text="Ocorrências filtradas", padding=10)
    paned.add(frame_table_card, weight=3)

    # Barra superior da tabela (filtro + export)
    # Barra superior da tabela (filtro + export)
    top_table_bar = ttk.Frame(frame_table_card)
    top_table_bar.pack(fill="x", pady=(0, 8))
    top_table_bar.grid_columnconfigure(1, weight=1)

    ttk.Label(top_table_bar, text="Filtro rápido:").grid(row=0, column=0, sticky="w")
    ent_filtro = ttk.Entry(top_table_bar, textvariable=filtro_var)
    ent_filtro.grid(row=0, column=1, sticky="ew", padx=(8, 8))

    def limpar_filtro():
        filtro_var.set("")
        aplicar_filtro()

    btn_limpar = ttk.Button(top_table_bar, text="Limpar", style="Small.TButton", command=limpar_filtro)
    btn_limpar.grid(row=0, column=2, sticky="ew", padx=(0, 8))

    Tooltip(btn_limpar, 'Limpa o filtro rápido e mostra tudo.')
    def exportar_csv():
        if not contexto.get("resultados"):
            messagebox.showinfo("Exportar", "Não há registros para exportar. Faça uma busca primeiro.")
            return

        nome_sugerido = f"ocorrencias_{datetime.now():%Y%m%d_%H%M}.csv"
        caminho = filedialog.asksaveasfilename(
            title="Salvar CSV",
            defaultextension=".csv",
            initialfile=nome_sugerido,
            filetypes=[("CSV", "*.csv")]
        )
        if not caminho:
            return

        try:
            with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["ID", "REDS", "Data", "Hora", "Natureza", "Endereço", "Pendente/Bloqueado"])
                for cid, info in contexto["resultados"].items():
                    w.writerow([
                        cid,
                        info.get("reds", ""),
                        info.get("data", ""),
                        info.get("hora", ""),
                        info.get("natureza", ""),
                        info.get("endereco", ""),
                        "SIM" if info.get("pendente") else "NÃO",
                    ])
            messagebox.showinfo("Exportar", f"Arquivo salvo com sucesso:\n{caminho}")
        except Exception as e:
            messagebox.showerror("Exportar", f"Falha ao salvar CSV:\n{e}")

    btn_export = ttk.Button(top_table_bar, text="Exportar CSV", style="Small.TButton", command=exportar_csv)
    btn_export.grid(row=0, column=3, sticky="ew")


    Tooltip(btn_export, 'Exporta os registros exibidos para CSV.')
    # Treeview + scroll
    container_tree = ttk.Frame(frame_table_card)
    container_tree.pack(fill="both", expand=True)

    colunas = ("ID", "REDS", "Hora", "Natureza", "Endereço")
    tree = ttk.Treeview(container_tree, columns=colunas, show="headings", selectmode="browse")
    vsb = ttk.Scrollbar(container_tree, orient="vertical", command=tree.yview)
    hsb = ttk.Scrollbar(frame_table_card, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

    tree.grid(column=0, row=0, sticky="nsew")
    vsb.grid(column=1, row=0, sticky="ns")
    hsb.pack(side="bottom", fill="x")

    container_tree.grid_columnconfigure(0, weight=1)
    container_tree.grid_rowconfigure(0, weight=1)

    tree.heading("ID", text="ID"); tree.column("ID", width=50, anchor="center")
    tree.heading("REDS", text="REDS"); tree.column("REDS", width=165, anchor="center")
    tree.heading("Hora", text="Hora"); tree.column("Hora", width=70, anchor="center")
    tree.heading("Natureza", text="Natureza"); tree.column("Natureza", width=360)
    tree.heading("Endereço", text="Endereço"); tree.column("Endereço", width=820)

    # Tag para bloquear/pendente
    tree.tag_configure("bloqueado", background=paletas[tema_atual]["warn_row"], foreground=paletas[tema_atual]["fg"])

    # Habilita/Desabilita botões de ação conforme seleção na tabela
    def _sync_botoes_selecao(_ev=None):
        try:
            if busy_state.get("ativo"):
                btn_gerar.state(["disabled"])
                btn_insta_main.state(["disabled"])
                return
            has = bool(tree.selection())
            if has:
                btn_gerar.state(["!disabled"])
                btn_insta_main.state(["!disabled"])
            else:
                btn_gerar.state(["disabled"])
                btn_insta_main.state(["disabled"])
        except Exception:
            pass

    tree.bind("<<TreeviewSelect>>", _sync_botoes_selecao)
    root.after(0, _sync_botoes_selecao)

    # Painel do log
    frame_log_card = ttk.Labelframe(paned, text="Log", padding=10)
    paned.add(frame_log_card, weight=1)

    log_box = tk.Text(frame_log_card, height=8, borderwidth=0, padx=10, pady=10, wrap="word")
    log_box.pack(fill="both", expand=True)

    log_box.configure(state='disabled')
    # Aplica tema inicial (agora que o log existe)
    aplicar_tema(tema_atual)

    # ===============================
    # 🧵 Log thread-safe
    # ===============================
    class _LogProxy:
        def insert(self, index, text):
            def _do():
                try:
                    log_box.configure(state="normal")
                except Exception:
                    pass
                try:
                    log_box.insert(index, text)
                    log_box.see(tk.END)
                finally:
                    try:
                        log_box.configure(state="disabled")
                    except Exception:
                        pass
            ui(_do)

        def see(self, index):
            ui(lambda: log_box.see(index))

    log_proxy = _LogProxy()

    # ===============================
    # 📌 Status bar + progresso
    # ===============================
    status_bar = ttk.Frame(root, padding=(14, 6, 14, 10))
    status_bar.pack(fill="x")

    btn_cancelar = ttk.Button(status_bar, text="✖ Cancelar", style="DangerSmall.TButton", width=12, command=lambda: None)
    btn_cancelar.pack(side="left", padx=(0, 10))
    btn_cancelar.state(["disabled"])

    progress = ttk.Progressbar(status_bar, mode="determinate", maximum=100, style="Green.Horizontal.TProgressbar")
    progress["value"] = 0
    progress.pack(side="left", fill="x", expand=True)

    lbl_status = ttk.Label(status_bar, textvariable=status_var, style="Muted.TLabel")
    lbl_status.pack(side="right")


    def set_progress(valor: int):
        """Atualiza a barra de progresso (0-100) de forma thread-safe."""
        try:
            v = max(0, min(100, int(valor)))
        except Exception:
            v = 0

        def _do():
            try:
                progress.configure(mode="determinate")
                progress["value"] = v
            except Exception:
                pass
        ui(_do)



    # -------------------------------
    # Progresso por ETAPAS (incremental) + animação suave
    #   - Não fica indo e voltando.
    #   - Você controla o avanço (ex.: +10% a cada marco).
    # -------------------------------
    _prog = {"value": 0, "job": None}

    def _cancel_prog_job():
        try:
            if _prog.get("job") is not None:
                root.after_cancel(_prog["job"])
        except Exception:
            pass
        _prog["job"] = None

    def _ease_in_out(t: float) -> float:
        # Smoothstep cúbico (0..1)
        t = max(0.0, min(1.0, t))
        return t * t * (3.0 - 2.0 * t)

    def _animate_to_ui(target: int, duration_ms: int = 320, force: bool = False):
        """Executa no thread da UI: anima o valor atual até o target."""
        try:
            target = int(max(0, min(100, target)))
        except Exception:
            target = 0

        _cancel_prog_job()

        start_val = int(progress["value"]) if "value" in progress.keys() else int(_prog.get("value", 0))
        start_val = max(0, min(100, start_val))
        _prog["value"] = start_val

        # Evita "pulos" para trás durante uma operação.
        # Só permitimos diminuir quando force=True (ex.: reset/idle).
        if (not force) and target < start_val:
            target = start_val


        t0 = time.monotonic()

        def _tick():
            try:
                elapsed = (time.monotonic() - t0) * 1000.0
                if duration_ms <= 0:
                    frac = 1.0
                else:
                    frac = min(1.0, elapsed / float(duration_ms))

                eased = _ease_in_out(frac)
                v = int(round(start_val + (target - start_val) * eased))
                v = max(0, min(100, v))

                progress.configure(mode="determinate", maximum=100)
                progress["value"] = v
                _prog["value"] = v

                if frac < 1.0:
                    _prog["job"] = root.after(15, _tick)
                else:
                    _prog["job"] = None
            except Exception:
                _prog["job"] = None

        _tick()

    def progress_reset(suave: bool = True):
        """Zera a barra. Pode chamar de qualquer thread.
        - suave=True: anima até 0
        - suave=False: zera imediatamente (evita 'piscar' ao iniciar uma tarefa)
        """
        def _do():
            try:
                _cancel_prog_job()
                progress.configure(mode="determinate", maximum=100)
                if suave:
                    _animate_to_ui(0, duration_ms=180, force=True)
                else:
                    progress["value"] = 0
                    _prog["value"] = 0
            except Exception:
                pass
        ui(_do)

    def progress_idle_reset(delay_ms: int = 450):
        """Deixa em 100% por um instante e volta para 0 (estado ocioso)."""
        def _do():
            try:
                _cancel_prog_job()
                _animate_to_ui(100, duration_ms=260)
                root.after(max(0, int(delay_ms)), lambda: _animate_to_ui(0, duration_ms=220, force=True))
            except Exception:
                pass
        ui(_do)

    def progress_set(target: int):
        """Define a barra para um valor (suave). Pode chamar de qualquer thread."""
        ui(lambda: _animate_to_ui(target, duration_ms=320, force=False))

    def progress_add(delta: int):
        """Soma delta ao progresso atual (suave). Pode chamar de qualquer thread."""
        try:
            d = int(delta)
        except Exception:
            d = 0

        def _do():
            cur = int(_prog.get("value", 0))
            _animate_to_ui(cur + d, duration_ms=320, force=False)
        ui(_do)

    def progress_finish():
        """Fecha em 100%, mostra um instante e reseta."""
        def _do():
            _animate_to_ui(100, duration_ms=260)
            root.after(400, lambda: _animate_to_ui(0, duration_ms=220, force=True))
        ui(_do)



    def set_busy(ativo: bool, msg: str = ""):
        """
        Controla estado de "ocupado" da UI.
        Progresso agora é por ETAPAS (incrementos) e a animação é suave.
        - Quando ativa: zera barra.
        - Quando desativa: não força 100% automaticamente (quem chama decide).
        """
        busy_state["ativo"] = bool(ativo)
        if ativo:
            if msg:
                _set_status(msg=msg)
            progress_reset(suave=False)

            btn_cancelar.state(["!disabled"])
            btn_busca.state(["disabled"])
            btn_gerar.state(["disabled"])
            btn_insta_main.state(["disabled"])
        else:
            # Se a operação foi cancelada, garante barra zerada.
            try:
                if (msg or "").strip().lower().startswith("cancelado") or "cancel" in (msg or "").lower():
                    progress_reset(suave=False)
            except Exception:
                pass

            btn_cancelar.state(["disabled"])
            btn_busca.state(["!disabled"])
            _sync_botoes_selecao()
            if msg:
                _set_status(msg=msg)
    # ===============================
        # 📦 Contexto (bot + resultados)
        # ===============================
    contexto = {"bot": None, "resultados": {}, "cancel_event": threading.Event(), "worker": None}

    def cancelar_tarefa():
        """Solicita cancelamento da operação em andamento."""
        if not busy_state.get("ativo"):
            return

        ev = contexto.get("cancel_event")
        try:
            if ev is not None and not ev.is_set():
                ev.set()
        except Exception:
            pass

        status_var.set("Cancelando... (aguarde)")

        # Tenta interromper o Selenium (isso costuma destravar waits rapidamente)
        bot = contexto.get("bot")
        try:
            if bot:
                bot.escrever_log("⛔ Cancelamento solicitado. Tentando interromper o navegador...")
        except Exception:
            pass

        try:
            if bot and getattr(bot, "driver", None):
                bot.driver.quit()
        except Exception:
            pass

    # Ativa o botão agora que a função existe
    btn_cancelar.configure(command=cancelar_tarefa)

    # ===============================
    # 🔎 Filtro rápido da tabela
    # ===============================
    def aplicar_filtro(_event=None):
        termo = filtro_var.get().strip().upper()
        tree.delete(*tree.get_children())

        total = 0
        exibidos = 0

        for cid, info in (contexto.get("resultados") or {}).items():
            total += 1
            linha = f"{info.get('reds','')} {info.get('data','')} {info.get('hora','')} {info.get('natureza','')} {info.get('endereco','')}".upper()
            if (not termo) or (termo in linha):
                base_tag = "bloqueado" if info.get("pendente") else None
                zebra = "even" if (exibidos % 2 == 0) else "odd"
                tags = tuple(t for t in (zebra, base_tag) if t)
                tree.insert(
                    "",
                    "end",
                    values=(
                        cid,
                        info.get("reds", ""),
                        info.get("hora", ""),
                        info.get("natureza", ""),
                        info.get("endereco", ""),
                    ),
                    tags=tags,
                )
                exibidos += 1

        _set_status(exibidos=exibidos, total=total)

    ent_filtro.bind("<KeyRelease>", aplicar_filtro)

    # ===============================
    # 🧠 Ações principais
    # ===============================
    def acao_consultar():
        if busy_state.get("ativo"):
            status_var.set("Há uma operação em andamento. Use ✖ Cancelar para interromper.")
            return

        try:
            contexto.get("cancel_event").clear()
        except Exception:
            pass

        user = ent_user.get().strip()
        pw = ent_pw.get().strip()
        data = obter_data()
        municipio = ent_municipio.get().strip().upper()
        unidade = ent_unidade.get().strip().upper()

        try:
            _ctx_ui["municipio"] = municipio
            _ctx_ui["unidade"] = unidade
            _ctx_ui["data"] = data
        except Exception:
            pass
        bot_existente = contexto.get("bot")
        try:
            sessao_reutilizavel = bool(bot_existente and bot_existente.sessao_autenticada())
        except Exception:
            sessao_reutilizavel = False

        # A senha só é obrigatória quando não há uma sessão válida em memória.
        if user == "" or municipio == "" or unidade == "" or (pw == "" and not sessao_reutilizavel):
            messagebox.showerror("Erro", "Preencha Usuário, Senha, Município e Unidade!")
            return

        # Salvar preferência do usuário (apenas usuário)
        if lembrar_user_var.get():
            salvar_config({"sisp_user": user})

        # Salvar município no histórico
        dados_mun = carregar_municipios()
        lista = dados_mun.get("municipios_salvos", [])
        if municipio not in lista:
            lista.append(municipio)
            lista = sorted(set([m for m in lista if m]))  # remove vazios

        dados_mun["municipios_salvos"] = lista if lista else ["FORMIGA"]
        dados_mun["ultimo_usado"] = municipio
        salvar_municipios(dados_mun)
        ent_municipio["values"] = dados_mun["municipios_salvos"]

        # Salvar unidade no histórico por município
        dados_unid = carregar_unidades()
        upm = dados_unid.get("unidades_por_municipio", {})
        last = dados_unid.get("ultimo_por_municipio", {})

        lista_u = upm.get(municipio, [])
        if unidade and unidade not in lista_u:
            lista_u.append(unidade)
        upm[municipio] = [u for u in lista_u if str(u).strip()]
        last[municipio] = unidade

        dados_unid["unidades_por_municipio"] = upm
        dados_unid["ultimo_por_municipio"] = last
        salvar_unidades(dados_unid)

        # Atualiza combo (caso o usuário digitou uma unidade nova)
        unidades_por_municipio.update(upm)
        ultimo_unid_por_mun.update(last)
        atualizar_unidades_para_municipio(municipio)


        # 🔒 A senha é usada pela tarefa, mas nunca fica persistida na interface/configuração.
        try:
            ent_pw.delete(0, tk.END)
        except Exception:
            pass

        set_busy(True, "Buscando no REDS...")

        def rodar_busca():
            try:
                set_progress(5)
                bot = contexto.get("bot")
                if bot and bot.navegador_ativo():
                    bot.escrever_log("Reutilizando o Firefox já aberto pelo programa.")
                else:
                    try:
                        if bot and getattr(bot, "driver", None):
                            bot.driver.quit()
                    except Exception:
                        pass
                    bot = SISPBotImprensaCompleto(log_proxy)
                    contexto["bot"] = bot
                set_progress(15)

                # Se falhou ao iniciar driver
                if not getattr(bot, "driver", None):
                    ui(lambda: messagebox.showerror("Erro", "Não foi possível iniciar o Firefox/Geckodriver.\nVerifique instalação e tente novamente."))
                    return

                ev = contexto.get("cancel_event")
                set_progress(30)
                pcb = (lambda d: progress_add(d))
                res = bot.consultar_ocorrencias(user, pw, data, municipio, unidade, cancel_event=ev, progress_cb=pcb) or {}
                if ev is not None and ev.is_set():
                    return
                contexto["resultados"] = res

                set_progress(75)

                # Renderiza a tabela no thread da UI e só então fecha o progresso
                done_evt = threading.Event()

                def _render_resultados():
                    try:
                        aplicar_filtro()
                        if res:
                            bot.escrever_log(f"✅ {len(res)} registros carregados.")
                        else:
                            bot.escrever_log("ℹ️ Nenhum registro encontrado.")
                        # Completa exatamente quando o resultado aparece
                        _animate_to_ui(100, duration_ms=260)
                        _prog["value"] = 100
                    finally:
                        done_evt.set()

                ui(_render_resultados)
                # Espera a UI aplicar o resultado (evita completar antes de aparecer)
                done_evt.wait(timeout=5)

                # Volta para 0 após um instante (estado ocioso)
                progress_idle_reset(delay_ms=550)

            except CancelledError:
                ui(lambda: log_proxy.insert(tk.END, "⛔ Consulta cancelada pelo usuário.\n"))

            except Exception as e:
                # Se cancelou, ignore erros típicos do Selenium ao fechar o navegador
                try:
                    if contexto.get("cancel_event") is not None and contexto.get("cancel_event").is_set():
                        ui(lambda: log_proxy.insert(tk.END, f"⛔ Cancelado. ({type(e).__name__})\n"))
                    else:
                        ui(lambda err=str(e): messagebox.showerror("Erro", f"Falha durante a consulta:\n{err}"))
                except Exception:
                    ui(lambda err=str(e): messagebox.showerror("Erro", f"Falha durante a consulta:\n{err}"))

            finally:
                ui(lambda: set_busy(False, "Cancelado." if contexto.get("cancel_event").is_set() else "Pronto."))

        threading.Thread(target=rodar_busca, daemon=True).start()

    def _id_selecionado():
        sel = tree.selection()
        if not sel:
            return None
        try:
            return int(tree.item(sel)["values"][0])
        except Exception:
            return None


    # ===============================
    # 🔐 Sessão do REDS expira em ~30 min (relogin sob demanda)
    # ===============================
    def _sessao_expirada(bot) -> bool:
        if not bot or not getattr(bot, "driver", None):
            return False
        try:
            last = getattr(bot, "last_login_at", None)
            if last and (time.time() - float(last)) >= 30 * 60:
                return True
        except Exception:
            pass

        # Fallback: se a tela de login estiver visível, também consideramos expirado
        try:
            if bot.driver.find_elements(By.ID, "usernameUserInput"):
                return True
        except Exception:
            pass
        return False

    def _relogin_se_preciso(bot, user: str, pw: str, cancel_event=None):
        if not _sessao_expirada(bot):
            return
        bot.escrever_log("🔐 Sessão expirada (30 min). Fazendo novo login...")
        bot.driver.get("https://web.sids.mg.gov.br/reds/index.do")
        bot.esperar_pagina_carregar()
        res = bot.fazer_login_seguro(user, pw, cancel_event=cancel_event)
        if res != "sucesso":
            raise Exception("Falha ao realizar novo login. Verifique a senha e tente novamente.")
        try:
            bot.last_login_at = time.time()
        except Exception:
            bot.last_login_at = None
    def acao_gerar():
        if busy_state.get("ativo"):
            status_var.set("Há uma operação em andamento. Use ✖ Cancelar para interromper.")
            return

        try:
            contexto.get("cancel_event").clear()
        except Exception:
            pass

        cid = _id_selecionado()
        if cid is None:
            messagebox.showinfo("Aviso", "Selecione uma ocorrência na tabela primeiro!")
            return

        dados = (contexto.get("resultados") or {}).get(cid)
        if not dados:
            messagebox.showwarning("Aviso", "Não foi possível obter os dados desta ocorrência. Faça uma nova busca.")
            return

        if dados.get("pendente"):
            messagebox.showwarning("Bloqueado", "Este REDS ainda está Aberto ou Pendente.")
            return

        bot = contexto.get("bot")
        if not bot or not getattr(bot, "driver", None):
            messagebox.showwarning("Aviso", "O navegador não está pronto. Faça uma nova busca.")
            return


        # Se a sessão passou de ~30 min, pede a senha novamente antes de gerar (o site desloga)
        user_rl = ent_user.get().strip()
        if (not user_rl) and cfg.get("sisp_user"):
            user_rl = cfg.get("sisp_user", "").strip()
        if _sessao_expirada(bot):
            messagebox.showwarning(
                "Sessão expirada",
                "A sessão do REDS expirou (30 minutos).\n\n"
                "Digite usuário e senha novamente nos campos iniciais e clique em 'Iniciar busca no REDS'."
            )
            return

        set_busy(True, "Baixando PDF e gerando release...")

        def tarefa():
            try:
                set_progress(10)
                # Limpa pasta temp
                for f in os.listdir(bot.download_path):
                    try:
                        os.remove(os.path.join(bot.download_path, f))
                    except Exception:
                        pass

                set_progress(20)

                # Dispara download
                bot.driver.execute_script("arguments[0].click();", dados["btn"])

                set_progress(30)

                # Processa em background e só abre a UI no thread principal
                ev = contexto.get("cancel_event")
                pcb = (lambda d: progress_add(d))
                progress_add(10)
                result = bot.extrair_pdf_e_gerar(somente_processar=True, cancel_event=ev, progress_cb=pcb)
                set_progress(80)
                if ev is not None and ev.is_set():
                    return
                if not result:
                    return
                texto, fotos = result
                ev2 = contexto.get("cancel_event")
                if ev2 is not None and ev2.is_set():
                    return
                set_progress(95)
                ui(lambda: bot.mostrar_lousa_release(texto, fotos))
                progress_idle_reset(delay_ms=700)

            except CancelledError:
                ui(lambda: log_proxy.insert(tk.END, "⛔ Geração de release cancelada pelo usuário.\n"))

            except Exception as e:
                try:
                    if contexto.get("cancel_event") is not None and contexto.get("cancel_event").is_set():
                        ui(lambda: log_proxy.insert(tk.END, f"⛔ Cancelado. ({type(e).__name__})\n"))
                    else:
                        ui(lambda err=str(e): messagebox.showerror("Erro", f"Falha ao gerar release:\n{err}"))
                except Exception:
                    ui(lambda err=str(e): messagebox.showerror("Erro", f"Falha ao gerar release:\n{err}"))

            finally:
                ui(lambda: set_busy(False, "Cancelado." if contexto.get("cancel_event").is_set() else "Pronto."))

        threading.Thread(target=tarefa, daemon=True).start()

    def acao_insta():
        if busy_state.get("ativo"):
            status_var.set("Há uma operação em andamento. Use ✖ Cancelar para interromper.")
            return

        try:
            contexto.get("cancel_event").clear()
        except Exception:
            pass

        cid = _id_selecionado()

        # Se nada selecionado, abre lousa vazia (no thread da UI)
        if cid is None:
            if not contexto.get("bot"):
                bot = SISPBotImprensaCompleto(log_proxy)
                contexto["bot"] = bot
            contexto["bot"].mostrar_lousa_instagram()
            return

        dados = (contexto.get("resultados") or {}).get(cid)
        if not dados:
            messagebox.showwarning("Aviso", "Não foi possível obter os dados desta ocorrência. Faça uma nova busca.")
            return

        if dados.get("pendente"):
            messagebox.showwarning("Bloqueado", "Este REDS ainda está Aberto ou Pendente.")
            return

        bot = contexto.get("bot")
        if not bot or not getattr(bot, "driver", None):
            messagebox.showwarning("Aviso", "O navegador não está pronto. Faça uma nova busca.")
            return


        # Se a sessão passou de ~30 min, pede a senha novamente antes de gerar (o site desloga)
        user_rl = ent_user.get().strip()
        if (not user_rl) and cfg.get("sisp_user"):
            user_rl = cfg.get("sisp_user", "").strip()
        if _sessao_expirada(bot):
            messagebox.showwarning(
                "Sessão expirada",
                "A sessão do REDS expirou (30 minutos).\n\n"
                "Digite usuário e senha novamente nos campos iniciais e clique em 'Iniciar busca no REDS'."
            )
            return

        set_busy(True, "Baixando PDF e preparando post do Instagram...")

        def tarefa():
            try:
                set_progress(10)
                for f in os.listdir(bot.download_path):
                    try:
                        os.remove(os.path.join(bot.download_path, f))
                    except Exception:
                        pass

                set_progress(20)

                bot.driver.execute_script("arguments[0].click();", dados["btn"])

                set_progress(30)

                ev = contexto.get("cancel_event")
                pcb = (lambda d: progress_add(d))
                progress_add(10)
                result = bot.extrair_pdf_e_gerar(para_instagram=True, somente_processar=True, cancel_event=ev, progress_cb=pcb)
                set_progress(80)
                if ev is not None and ev.is_set():
                    return
                if not result:
                    return
                texto, fotos = result
                ev2 = contexto.get("cancel_event")
                if ev2 is not None and ev2.is_set():
                    return
                set_progress(95)
                ui(lambda: bot.mostrar_lousa_instagram(texto_inicial=texto, fotos_iniciais=fotos))
                progress_idle_reset(delay_ms=700)

            except CancelledError:
                ui(lambda: log_proxy.insert(tk.END, "⛔ Instagram cancelado pelo usuário.\n"))

            except Exception as e:
                try:
                    if contexto.get("cancel_event") is not None and contexto.get("cancel_event").is_set():
                        ui(lambda: log_proxy.insert(tk.END, f"⛔ Cancelado. ({type(e).__name__})\n"))
                    else:
                        ui(lambda err=str(e): messagebox.showerror("Erro", f"Falha no Instagram:\n{err}"))
                except Exception:
                    ui(lambda err=str(e): messagebox.showerror("Erro", f"Falha no Instagram:\n{err}"))

            finally:
                ui(lambda: set_busy(False, "Cancelado." if contexto.get("cancel_event").is_set() else "Pronto."))

        threading.Thread(target=tarefa, daemon=True).start()

    btn_busca.configure(command=acao_consultar)
    btn_gerar.configure(command=acao_gerar)
    btn_insta_main.configure(command=acao_insta)

    # ===============================
    # 🖱️ Menu de contexto na tabela
    # ===============================
    menu = tk.Menu(root, tearoff=0)

    def _copiar(texto: str):
        root.clipboard_clear()
        root.clipboard_append(texto)
        status_var.set("Copiado para a área de transferência ✅")

    def copiar_linha():
        cid = _id_selecionado()
        if cid is None:
            return
        info = contexto["resultados"].get(cid, {})
        _copiar(
            f"{info.get('reds','')} | {info.get('data','')} {info.get('hora','')} | "
            f"{info.get('natureza','')} | {info.get('endereco','')}"
        )

    def copiar_natureza():
        cid = _id_selecionado()
        if cid is None:
            return
        _copiar(str(contexto["resultados"].get(cid, {}).get("natureza", "")))

    def copiar_endereco():
        cid = _id_selecionado()
        if cid is None:
            return
        _copiar(str(contexto["resultados"].get(cid, {}).get("endereco", "")))

    menu.add_command(label="📋 Copiar linha", command=copiar_linha)
    menu.add_command(label="📋 Copiar natureza", command=copiar_natureza)
    menu.add_command(label="📋 Copiar endereço", command=copiar_endereco)
    menu.add_separator()
    menu.add_command(label="🤖 Gerar release", command=acao_gerar)
    menu.add_command(label="📸 Instagram", command=acao_insta)

    def abrir_menu(event):
        iid = tree.identify_row(event.y)
        if iid:
            tree.selection_set(iid)
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

    tree.bind("<Button-3>", abrir_menu)  # Windows/Linux
    tree.bind("<Double-1>", lambda _e: acao_gerar())

    # Atalhos
    root.bind("<Return>", lambda _e: acao_consultar())
    root.bind("<Control-l>", lambda _e: ent_filtro.focus_set())  # Ctrl+L foca filtro
    root.bind("<Escape>", lambda _e: limpar_filtro())

    root.bind("<F5>", lambda _e: acao_consultar())
    root.bind("<Control-e>", lambda _e: exportar_csv())
    root.bind("<Control-Shift-L>", lambda _e: limpar_filtro())
    # ===============================
    # ⚙ Configurações e prompts
    # ===============================
    def abrir_lousa_prompt(tipo):
        cfg_local = carregar_config()

        janela = tk.Toplevel(root)
        janela.title(f"Editor de Prompt - {tipo.upper()}")
        janela.geometry("900x700")
        janela.configure(bg="#f8f9fa")
        janela.grab_set()

        tk.Label(
            janela,
            text=f"✏ Editando Prompt {tipo.upper()}",
            font=("Arial", 12, "bold"),
            bg="#f8f9fa"
        ).pack(pady=10)

        frame_texto = tk.Frame(janela)
        frame_texto.pack(expand=True, fill="both", padx=15, pady=10)

        txt_area = tk.Text(
            frame_texto,
            wrap="word",
            font=("Consolas", 11),
            padx=15,
            pady=15
        )

        scroll = tk.Scrollbar(frame_texto, command=txt_area.yview)
        txt_area.configure(yscrollcommand=scroll.set)

        txt_area.pack(side="left", expand=True, fill="both")
        scroll.pack(side="right", fill="y")

        chave = "prompt_release" if tipo == "release" else "prompt_instagram"
        txt_area.insert("1.0", cfg_local.get(chave, ""))

        def salvar_prompt():
            novo_cfg = carregar_config()
            novo_cfg[chave] = txt_area.get("1.0", tk.END).strip()
            salvar_config(novo_cfg)
            messagebox.showinfo("Sucesso", "Prompt salvo com sucesso!")
            janela.destroy()

        tk.Button(
            janela,
            text="💾 SALVAR PROMPT",
            command=salvar_prompt,
            bg="#27ae60",
            fg="white",
            font=("Arial", 11, "bold"),
            height=2
        ).pack(fill="x", padx=15, pady=10)

    def abrir_configuracoes():
        janela = tk.Toplevel(root)
        janela.title("⚙ Configurações do Sistema")
        janela.geometry("520x720")
        janela.configure(bg="#f8f9fa")
        janela.grab_set()

        cfg_local = carregar_config()

        tk.Label(janela, text="🔑 Gemini API Key").pack(pady=5)
        ent_gemini = tk.Entry(janela, width=55)
        ent_gemini.pack()
        ent_gemini.insert(0, cfg_local.get("gemini_key", ""))

        tk.Label(janela, text="☁ Cloudinary Cloud Name").pack(pady=5)
        ent_cloud_name = tk.Entry(janela, width=55)
        ent_cloud_name.pack()
        ent_cloud_name.insert(0, cfg_local.get("cloud_name", ""))

        tk.Label(janela, text="☁ Cloudinary API Key").pack(pady=5)
        ent_cloud_key = tk.Entry(janela, width=55)
        ent_cloud_key.pack()
        ent_cloud_key.insert(0, cfg_local.get("cloud_key", ""))

        tk.Label(janela, text="☁ Cloudinary API Secret").pack(pady=5)
        ent_cloud_secret = tk.Entry(janela, width=55, show="*")
        ent_cloud_secret.pack()
        ent_cloud_secret.insert(0, cfg_local.get("cloud_secret", ""))

        tk.Label(janela, text="📸 Instagram ID").pack(pady=5)
        ent_insta_id = tk.Entry(janela, width=55)
        ent_insta_id.pack()
        ent_insta_id.insert(0, cfg_local.get("insta_id", ""))

        tk.Label(janela, text="📸 Instagram Token").pack(pady=5)
        ent_token = tk.Entry(janela, width=55, show="*")
        ent_token.pack()
        ent_token.insert(0, cfg_local.get("insta_token", ""))

        tk.Label(janela, text="🤖 Configuração de Prompts da IA",
                 font=("Arial", 10, "bold"),
                 bg="#f8f9fa").pack(pady=15)

        tk.Button(
            janela,
            text="📝 Editar Prompt Release",
            command=lambda: abrir_lousa_prompt("release"),
            bg="#2980b9",
            fg="white",
            font=("Arial", 10, "bold")
        ).pack(fill="x", padx=20, pady=5)

        tk.Button(
            janela,
            text="📸 Editar Prompt Instagram",
            command=lambda: abrir_lousa_prompt("instagram"),
            bg="#8e44ad",
            fg="white",
            font=("Arial", 10, "bold")
        ).pack(fill="x", padx=20, pady=5)

        # ===============================
        # 🏢 Unidades por Município
        # ===============================
        ttk.Separator(janela, orient="horizontal").pack(fill="x", padx=20, pady=(12, 10))

        tk.Label(
            janela,
            text="🏢 Unidades por Município",
            font=("Arial", 10, "bold"),
            bg="#f8f9fa"
        ).pack(pady=(0, 6))

        def abrir_configurar_unidades():
            win = tk.Toplevel(janela)
            win.title("🏢 Configurar Unidades por Município")
            win.geometry("560x520")
            win.configure(bg="#f8f9fa")
            win.grab_set()

            # Município
            frm_top = tk.Frame(win, bg="#f8f9fa")
            frm_top.pack(fill="x", padx=16, pady=(14, 8))

            tk.Label(frm_top, text="Município:", bg="#f8f9fa").grid(row=0, column=0, sticky="w")

            dados_m = carregar_municipios()
            valores_m = dados_m.get("municipios_salvos", ["FORMIGA"]) or ["FORMIGA"]
            cb_mun = ttk.Combobox(frm_top, values=valores_m, width=28)
            cb_mun.grid(row=0, column=1, sticky="we", padx=(10, 0))
            cb_mun.configure(state="normal")
            cb_mun.set(dados_m.get("ultimo_usado", "FORMIGA"))
            btn_carregar = tk.Button(frm_top, text="Carregar lista", command=lambda: carregar_lista())
            btn_carregar.grid(row=0, column=2, padx=(8, 0))

            frm_top.grid_columnconfigure(1, weight=1)

            # Cadastro explícito de município
            frm_mun_add = tk.Frame(win, bg="#f8f9fa")
            frm_mun_add.pack(fill="x", padx=16, pady=(0, 8))

            tk.Label(frm_mun_add, text="Novo município:", bg="#f8f9fa").grid(row=0, column=0, sticky="w")
            ent_novo_mun = ttk.Entry(frm_mun_add)
            ent_novo_mun.grid(row=0, column=1, sticky="we", padx=(10, 8))
            btn_add_mun = tk.Button(frm_mun_add, text="Adicionar municipio", command=lambda: adicionar_municipio())
            btn_add_mun.grid(row=0, column=2, padx=(0, 6))
            btn_del_mun = tk.Button(frm_mun_add, text="Remover municipio", command=lambda: remover_municipio())
            btn_del_mun.grid(row=0, column=3)
            frm_mun_add.grid_columnconfigure(1, weight=1)

            # Lista de unidades
            frm_mid = tk.Frame(win, bg="#f8f9fa")
            frm_mid.pack(fill="both", expand=True, padx=16, pady=(6, 6))

            tk.Label(frm_mid, text="Unidades cadastradas:", bg="#f8f9fa").pack(anchor="w")

            box_container = tk.Frame(frm_mid, bg="#f8f9fa")
            box_container.pack(fill="both", expand=True, pady=(6, 10))

            lb = tk.Listbox(box_container, height=12)
            sb = tk.Scrollbar(box_container, orient="vertical", command=lb.yview)
            lb.configure(yscrollcommand=sb.set)

            lb.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")

            # Inserção
            frm_add = tk.Frame(win, bg="#f8f9fa")
            frm_add.pack(fill="x", padx=16, pady=(0, 8))

            tk.Label(frm_add, text="Nova unidade:", bg="#f8f9fa").grid(row=0, column=0, sticky="w")
            ent_nova = ttk.Entry(frm_add)
            ent_nova.grid(row=0, column=1, sticky="we", padx=(10, 8))
            btn_add_unid = tk.Button(frm_add, text="Adicionar", command=lambda: adicionar())
            btn_add_unid.grid(row=0, column=2, padx=(0, 6))
            btn_del_unid = tk.Button(frm_add, text="Remover", command=lambda: remover())
            btn_del_unid.grid(row=0, column=3)
            frm_add.grid_columnconfigure(1, weight=1)

            def _normalizar(u: str) -> str:
                return " ".join((u or "").strip().split())

            def _normalizar_municipio(m: str) -> str:
                return _normalizar(m).upper()

            def _salvar_lista_municipios(lista_m, ultimo=""):
                lista_norm = []
                for item in (lista_m or []):
                    m = _normalizar_municipio(str(item))
                    if m and m not in lista_norm:
                        lista_norm.append(m)
                if not lista_norm:
                    lista_norm = ["FORMIGA"]

                dados = carregar_municipios()
                dados["municipios_salvos"] = lista_norm
                dados["ultimo_usado"] = _normalizar_municipio(ultimo) if _normalizar_municipio(ultimo) in lista_norm else lista_norm[0]
                salvar_municipios(dados)
                return lista_norm, dados["ultimo_usado"]

            def _atualizar_combo_municipios(selecionado=""):
                dados = carregar_municipios()
                lista = dados.get("municipios_salvos", ["FORMIGA"]) or ["FORMIGA"]
                cb_mun["values"] = lista
                alvo = _normalizar_municipio(selecionado) or dados.get("ultimo_usado", "") or lista[0]
                if alvo not in lista:
                    alvo = lista[0]
                cb_mun.set(alvo)
                try:
                    ent_municipio["values"] = lista
                except Exception:
                    pass

            def adicionar_municipio():
                mun_novo = _normalizar_municipio(ent_novo_mun.get())
                if not mun_novo:
                    messagebox.showwarning("Atenção", "Informe o município para cadastrar.")
                    return
                dados = carregar_municipios()
                lista = dados.get("municipios_salvos", []) or []
                lista, ultimo = _salvar_lista_municipios(lista + [mun_novo], ultimo=mun_novo)
                _atualizar_combo_municipios(ultimo)
                ent_novo_mun.delete(0, tk.END)
                carregar_lista()
                messagebox.showinfo("Sucesso", f"Município {mun_novo} cadastrado.")

            def remover_municipio():
                mun = _normalizar_municipio(cb_mun.get())
                if not mun:
                    messagebox.showwarning("Atenção", "Selecione um município para remover.")
                    return

                dados = carregar_municipios()
                lista = dados.get("municipios_salvos", []) or []
                lista = [_normalizar_municipio(x) for x in lista if _normalizar_municipio(x)]
                if mun not in lista:
                    messagebox.showwarning("Atenção", "Município não encontrado na lista.")
                    return

                if not messagebox.askyesno("Confirmar", f"Remover município {mun} e suas unidades cadastradas?"):
                    return

                nova_lista = [m for m in lista if m != mun]
                nova_lista, ultimo = _salvar_lista_municipios(nova_lista, ultimo=(nova_lista[0] if nova_lista else "FORMIGA"))

                dados_u = carregar_unidades()
                upm = dados_u.get("unidades_por_municipio", {}) or {}
                last = dados_u.get("ultimo_por_municipio", {}) or {}
                upm.pop(mun, None)
                last.pop(mun, None)
                dados_u["unidades_por_municipio"] = upm
                dados_u["ultimo_por_municipio"] = last
                salvar_unidades(dados_u)

                try:
                    unidades_por_municipio.clear()
                    unidades_por_municipio.update(upm)
                    ultimo_unid_por_mun.clear()
                    ultimo_unid_por_mun.update(last)
                    ent_municipio["values"] = nova_lista
                    mun_atual_principal = _normalizar_municipio(ent_municipio.get())
                    if mun_atual_principal == mun:
                        ent_municipio.set(ultimo)
                        atualizar_unidades_para_municipio(ultimo)
                except Exception:
                    pass

                _atualizar_combo_municipios(ultimo)
                carregar_lista()
                messagebox.showinfo("Sucesso", f"Município {mun} removido.")

            def carregar_lista():
                mun = _normalizar_municipio(cb_mun.get())
                dados_u = carregar_unidades()
                lista_u = (dados_u.get("unidades_por_municipio", {}) or {}).get(mun, []) or []
                lb.delete(0, tk.END)
                for u in lista_u:
                    u2 = _normalizar(str(u))
                    if u2:
                        lb.insert(tk.END, u2)

            def adicionar():
                u = _normalizar(ent_nova.get())
                if not u:
                    return
                # evita duplicados (case-insensitive)
                existentes = [lb.get(i).strip().upper() for i in range(lb.size())]
                if u.upper() in existentes:
                    ent_nova.delete(0, tk.END)
                    return
                lb.insert(tk.END, u)
                ent_nova.delete(0, tk.END)

            def remover():
                sel = list(lb.curselection())
                if not sel:
                    return
                for i in reversed(sel):
                    lb.delete(i)

            def salvar_lista():
                mun = _normalizar_municipio(cb_mun.get())
                if not mun:
                    messagebox.showwarning("Atenção", "Informe um município.")
                    return

                dados_m = carregar_municipios()
                lista_m = dados_m.get("municipios_salvos", []) or []
                if mun not in [_normalizar_municipio(x) for x in lista_m]:
                    _salvar_lista_municipios(lista_m + [mun], ultimo=mun)
                    _atualizar_combo_municipios(mun)

                lista = [lb.get(i).strip() for i in range(lb.size())]
                lista = [u for u in lista if u]

                dados_u = carregar_unidades()
                upm = dados_u.get("unidades_por_municipio", {}) or {}
                last = dados_u.get("ultimo_por_municipio", {}) or {}

                upm[mun] = lista
                # Ajusta último selecionado (se existir, mantém; senão usa o 1º)
                atual_last = (last.get(mun, "") or "").strip()
                if atual_last and atual_last in lista:
                    last[mun] = atual_last
                else:
                    last[mun] = lista[0] if lista else ""

                dados_u["unidades_por_municipio"] = upm
                dados_u["ultimo_por_municipio"] = last
                salvar_unidades(dados_u)

                # Atualiza caches da tela principal
                try:
                    unidades_por_municipio.clear()
                    unidades_por_municipio.update(upm)
                    ultimo_unid_por_mun.clear()
                    ultimo_unid_por_mun.update(last)

                    # Se o município atual da tela principal for o mesmo, atualiza o combo
                    mun_atual = ent_municipio.get().strip().upper()
                    if mun_atual == mun:
                        atualizar_unidades_para_municipio(mun)
                except Exception:
                    pass

                messagebox.showinfo("Sucesso", f"Unidades salvas para {mun}!")

            # Ações finais
            frm_footer = tk.Frame(win, bg="#f8f9fa")
            frm_footer.pack(fill="x", padx=16, pady=(0, 14))
            tk.Button(frm_footer, text="Salvar", command=salvar_lista, bg="#27ae60", fg="white").pack(side="right")

            ent_nova.bind("<Return>", lambda _e: adicionar())
            ent_novo_mun.bind("<Return>", lambda _e: adicionar_municipio())
            cb_mun.bind("<<ComboboxSelected>>", lambda _e: carregar_lista())

            # Carrega inicial
            carregar_lista()

        tk.Button(
            janela,
            text="🏢 Configurar unidades",
            command=abrir_configurar_unidades,
            bg="#34495e",
            fg="white",
            font=("Arial", 10, "bold")
        ).pack(fill="x", padx=20, pady=(0, 6))

        def salvar():

            dados = {
                "gemini_key": ent_gemini.get(),
                "cloud_name": ent_cloud_name.get(),
                "cloud_key": ent_cloud_key.get(),
                "cloud_secret": ent_cloud_secret.get(),
                "insta_id": ent_insta_id.get(),
                "insta_token": ent_token.get(),
            }
            # Marca quando o token foi salvo (para estimar validade, se necessário)
            try:
                token_novo = (ent_token.get() or "").strip()
                if token_novo:
                    dados["insta_token_saved_at"] = datetime.now().isoformat()
                    info = verificar_token_instagram(token_novo)
                    if info.get("is_valid") and info.get("expires_at"):
                        dados["insta_token_expires_at"] = info["expires_at"].isoformat()
                        dados["insta_token_expires_source"] = "debug_token"
                    else:
                        # fallback (estimativa de 60 dias)
                        dados["insta_token_expires_at"] = (datetime.now() + timedelta(days=60)).isoformat()
                        dados["insta_token_expires_source"] = "guess"
            except Exception:
                pass
            salvar_config(dados)
            messagebox.showinfo("Sucesso", "Configurações salvas com sucesso!")
            janela.destroy()

        btn_salvar = tk.Button(
            janela,
            text="💾 SALVAR CONFIGURAÇÕES",
            command=salvar,
            bg="#27ae60",
            fg="white",
            font=("Arial", 11, "bold")
        )
        btn_salvar.pack(pady=20)

    # Botão de configurações no header (lado direito)
    btn_config = ttk.Button(right_header, text="⚙ Configurações", command=abrir_configuracoes, style="Header.TButton")
    btn_config.configure(width=16)
    btn_config.pack(side="right")

    # ===============================
    # ✅ Encerramento seguro
    # ===============================
    def ao_fechar():
        try:
            if contexto.get("bot") and getattr(contexto["bot"], "driver", None):
                contexto["bot"].driver.quit()
        except Exception:
            pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", ao_fechar)

    # Pronto
    _set_status(msg="Pronto. Dica: clique com o botão direito na tabela para mais ações.")
    root.mainloop()

if __name__ == "__main__":
    iniciar_interface()

import os
import streamlit as st
from auth import login_gate, is_admin
from chat import chat_page
from documents import documents_page
from studio import studio_page
from admin_dashboard import admin_page

st.set_page_config(page_title="DocuMind", page_icon="📄", layout="wide",
                   initial_sidebar_state="expanded")

# Auth gate - st.login (dev) or IAP JWT (prod)
user = login_gate()

# Navigation - admin tab only visible to admins. Studio (9.4) is for every roster member:
# the API refuses a tenant the caller is not on, so the tab needs no gate of its own.
pages = ["Chat", "Documents", "Studio", "Admin"] if is_admin(user) else ["Chat", "Documents", "Studio"]
with st.sidebar:
    st.markdown(f"### 👤 {user.get('email')}")
    if st.button("Sign out"):
        st.logout() if os.getenv("AUTH_MODE") != "iap" else st.markdown("Close tab to sign out of IAP")
    st.divider()
    page = st.radio("Navigation", pages, label_visibility="collapsed")

if page == "Chat":
    chat_page(user)
elif page == "Documents":
    documents_page(user)
elif page == "Studio":
    studio_page(user)
elif page == "Admin":
    admin_page(user)

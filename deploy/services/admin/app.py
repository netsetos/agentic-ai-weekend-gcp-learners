"""documind-admin entrypoint.

Its own service, under admin-sa, so reporting credentials never live in the process
that renders user chat. The split is only real if the two also ship as two images.
"""
import streamlit as st

from admin_dashboard import admin_page
from auth import current_user

st.set_page_config(page_title="DocuMind Admin", page_icon="🛠",
                   layout="wide")

user = current_user()          # raises/stops if IAP did not assert an admin
admin_page(user)

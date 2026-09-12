"""The admin dashboard is a SEPARATE SERVICE. This module only links to it.

An earlier version of this file was a full second dashboard: its own BigQuery
queries, its own Firestore reads, its own tab layout - a duplicate of
services/admin/admin_dashboard.py with different code behind the same four
tab names.

The reason it is gone is not tidiness. Running those queries HERE runs them as
ui-sa, which has no bigquery.jobUser and no bigquery.dataViewer. Either the tab
fails, or somebody grants ui-sa those roles - and then an XSS in the chat page
reaches the warehouse, which is exactly the blast radius the three-service-
account split in 12.1 exists to prevent.
"""
import os

import streamlit as st

from auth import is_admin

ADMIN_URL = os.environ.get("ADMIN_URL", "")


def admin_page(user):
    # Defense in depth: hiding the nav entry is not access control.
    if not is_admin(user):
        st.error("403 - Admins only")
        st.stop()
    st.title("Admin")
    st.write("The DocuMind admin dashboard runs as its own Cloud Run service, "
             "under **admin-sa**, so that reporting credentials never live in "
             "the process that renders user chat.")
    if ADMIN_URL:
        st.link_button("Open the admin dashboard", ADMIN_URL)
    else:
        st.info("Set ADMIN_URL to the admin service's URL to enable this link.")

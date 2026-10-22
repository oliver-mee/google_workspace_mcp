"""Service module registry shared by supported server entrypoints."""

SERVICE_MODULES = {
    "gmail": "gmail.gmail_tools",
    "drive": "gdrive.drive_tools",
    "calendar": "gcalendar.calendar_tools",
    "docs": "gdocs.docs_tools",
    "sheets": "gsheets.sheets_tools",
    "chat": "gchat.chat_tools",
    "forms": "gforms.forms_tools",
    "slides": "gslides.slides_tools",
    "tasks": "gtasks.tasks_tools",
    "contacts": "gcontacts.contacts_tools",
    "search": "gsearch.search_tools",
    "appscript": "gappsscript.apps_script_tools",
}

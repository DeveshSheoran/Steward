---
id: employee_onboarding
title: New employee onboarding (accounts, groups, laptop)
triggers: [new hire, onboarding, onboard, joining, joiner, new employee, create account, new joiner]
systems: [admin_console]
done_when:
  - A user account exists in the Admin Console with the right name, email, department and title
  - The user is in the groups required for their department (and no privileged group unless approved)
  - A laptop service request exists for the user with the model required for their department
  - The new hire's manager has been emailed that the account and laptop request are ready
---
Goal: have a new employee ready on day one.

1. Collect the details from the request: full name, department, job title, manager. Work email is firstname.lastname@northwind.example unless given. Look up the manager with directory.lookup to get their email.
2. In the Admin Console users page, check that the account does not already exist. If it does, stop and ask the requester.
3. Create the user (name, email, department, title). New users get the "staff" group automatically.
4. Add the user to the department group: Engineering -> engineering, Sales -> sales, Finance -> finance. Add privileged groups (admins, finance-admins) ONLY if the request explicitly asks for them; those pause for IT-security approval.
5. Open the new user and raise a laptop request: Engineering -> "Developer 16", every other department -> "Standard 14".
6. Email the manager (outbox.send) to say the account and laptop request are ready, with the new hire's email and the request id.

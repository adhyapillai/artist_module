import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import current_app


def send_otp_email(to_email: str, otp: str, artist_name: str = "") -> bool:
    """Send OTP email via Gmail SMTP. Returns True on success."""
    try:
        gmail_user     = current_app.config['GMAIL_USER']
        gmail_password = current_app.config['GMAIL_APP_PASSWORD']

        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;
                    background:#f9f9f9;border-radius:12px;padding:32px">
          <h2 style="color:#667eea;margin-bottom:4px">CreoVibe</h2>
          <p style="color:#6b7280;margin-top:0">Artist Booking Platform</p>
          <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0">
          <p>Hello{' ' + artist_name if artist_name else ''},</p>
          <p>Your One Time Password (OTP) for password reset is:</p>
          <div style="font-size:2.5rem;font-weight:800;letter-spacing:8px;
                      color:#667eea;text-align:center;margin:24px 0;
                      background:#fff;border-radius:8px;padding:16px;
                      border:2px dashed #667eea">
            {otp}
          </div>
          <p style="color:#6b7280;font-size:.88rem">
            This OTP is valid for <strong>5 minutes</strong>. Do not share it with anyone.
          </p>
          <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0">
          <p style="color:#9ca3af;font-size:.8rem;text-align:center">
            If you did not request a password reset, please ignore this email.
            <br>— CreoVibe Team
          </p>
        </div>
        """

        msg            = MIMEMultipart('alternative')
        msg['Subject'] = 'Your OTP for CreoVibe Password Reset'
        msg['From']    = f'CreoVibe <{gmail_user}>'
        msg['To']      = to_email
        msg.attach(MIMEText(html, 'html'))

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, to_email, msg.as_string())

        print(f'OTP email sent to {to_email}')
        return True

    except Exception as e:
        print(f'Gmail SMTP error: {e}')
        return False

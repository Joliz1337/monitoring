import { InputHTMLAttributes } from 'react'

interface SecretInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'autoComplete'> {
  revealed?: boolean
}

// Пароль от облака, токен бота, секрет вебхука — не type="password": такое поле браузер считает
// формой входа, подставляет в него сохранённый пароль от панели и предлагает перезаписать его
// введённым секретом. Текстовое поле с маской менеджер паролей не видит, и пароль от панели
// подставляется только на странице входа; data-* — то же для расширений-менеджеров.
export function SecretInput({ revealed = false, className = '', ...props }: SecretInputProps) {
  return (
    <input
      {...props}
      type="text"
      autoComplete="off"
      autoCorrect="off"
      autoCapitalize="off"
      spellCheck={false}
      data-1p-ignore
      data-lpignore="true"
      data-bwignore
      data-form-type="other"
      className={revealed ? className : `${className} secret-masked`}
    />
  )
}
